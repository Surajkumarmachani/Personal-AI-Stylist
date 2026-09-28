"""Poll a Gmail inbox for forwarded order emails (Phase 15, no-domain mode).

When there is no domain to receive mail on, one dedicated Gmail account is the
inbox: users forward to <inbox>+<token>@gmail.com, and plus-addressing delivers
every +token to that one mailbox. This cron reads new mail over IMAP with an
app password and hands each message to the same intake the webhook uses
(stylist_api.purchase_intake.accept_email).

WHY IMAP AND NOT THE GMAIL API
------------------------------
The Gmail API's read scope is "restricted": outside Testing mode it needs
Google verification and a paid security assessment, and in Testing mode its
refresh tokens expire every 7 days. This app only ever reads its OWN inbox, so
IMAP with an app password (2-step verification on, Settings → App passwords)
has none of that, and no token to renew.

WHICH ADDRESS WAS IT SENT TO
----------------------------
A Gmail auto-forward keeps the ORIGINAL headers, so `To:` is still the user's
own address; the +token survives in `Delivered-To:` (and sometimes
`X-Forwarded-To:`). Every recipient-ish header is offered to the token parser.

A message is marked \\Seen only after intake succeeded, so a failure is retried
on the next tick instead of being lost.
"""

from __future__ import annotations

import asyncio
import imaplib
import logging
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Any

from stylist_api.purchase_intake import accept_email
from stylist_api.settings import get_settings
from stylist_shop.order_email import addresses

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
BATCH = 25
RECIPIENT_HEADERS = ("Delivered-To", "X-Forwarded-To", "X-Original-To", "To", "Cc")


def fields_from_message(raw: bytes) -> dict[str, Any]:
    """The intake fields for one raw RFC 822 message. Pure."""
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    assert isinstance(msg, EmailMessage)
    recipients: list[str] = []
    for header in RECIPIENT_HEADERS:
        for value in msg.get_all(header, []):
            recipients += addresses(str(value))

    def part(kind: str) -> str | None:
        body = msg.get_body(preferencelist=(kind,))
        if body is None:
            return None
        try:
            return str(body.get_content())
        except (LookupError, UnicodeDecodeError):
            payload = body.get_payload(decode=True)
            return payload.decode("utf-8", "replace") if isinstance(payload, bytes) else None

    message_id = str(msg.get("Message-ID") or "").strip()
    return {
        "recipients": recipients,
        "sender_header": str(msg.get("From") or ""),
        "subject": str(msg.get("Subject") or ""),
        "html": part("html"),
        "plain": part("plain"),
        "headers": f"Message-ID: {message_id}\n" if message_id else "",
    }


def _fetch_unseen(user: str, password: str) -> list[tuple[bytes, bytes]]:
    with imaplib.IMAP4_SSL(IMAP_HOST, timeout=30) as imap:
        imap.login(user, password)
        imap.select("INBOX")
        _, data = imap.uid("search", "UNSEEN")
        uids = (data[0] or b"").split()[:BATCH]
        messages: list[tuple[bytes, bytes]] = []
        for uid in uids:
            # BODY.PEEK so fetching does not mark it read; that happens only
            # after intake succeeded (see _mark_seen).
            _, fetched = imap.uid("fetch", uid.decode(), "(BODY.PEEK[])")
            for item in fetched:
                if isinstance(item, tuple) and isinstance(item[1], bytes):
                    messages.append((uid, item[1]))
        return messages


def _mark_seen(user: str, password: str, uids: list[bytes]) -> None:
    if not uids:
        return
    with imaplib.IMAP4_SSL(IMAP_HOST, timeout=30) as imap:
        imap.login(user, password)
        imap.select("INBOX")
        imap.uid("store", b",".join(uids).decode(), "+FLAGS", "(\\Seen)")


async def poll_order_inbox(ctx: dict[str, Any]) -> dict[str, Any]:
    """One tick (arq cron, every minute)."""
    settings = get_settings()
    user, password = settings.inbound_gmail_address, settings.inbound_gmail_app_password
    if not (user and password):
        return {"skipped": "no gmail inbox configured"}

    try:
        messages = await asyncio.to_thread(_fetch_unseen, user, password)
    except (imaplib.IMAP4.error, OSError) as exc:
        # A wrong app password or a Gmail outage: log and try next minute.
        logger.warning("order inbox poll failed: %s", exc)
        return {"error": type(exc).__name__}

    done: list[bytes] = []
    outcomes: dict[str, int] = {}
    for uid, raw in messages:
        try:
            result = await accept_email(**fields_from_message(raw), settings=settings)
        except Exception:
            logger.exception("order email %s could not be accepted; will retry", uid)
            continue
        key = str(result.get("status") or result.get("reason") or "duplicate")
        outcomes[key] = outcomes.get(key, 0) + 1
        done.append(uid)

    try:
        await asyncio.to_thread(_mark_seen, user, password, done)
    except (imaplib.IMAP4.error, OSError) as exc:
        # Worst case the same messages are read again next minute, and the
        # intake dedupes them on Message-ID.
        logger.warning("could not mark order emails seen: %s", exc)
    return {"read": len(messages), **outcomes}
