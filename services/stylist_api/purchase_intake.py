"""Accept one received order email: find its owner, classify, store, queue.

Shared by the two ways mail arrives, so they cannot drift apart:
  - the /inbound/email webhook (Cloudflare Email Worker, or SendGrid), and
  - the Gmail inbox poller in the worker (stylist_worker/inbox_poll.py),
    used when there is no domain: users forward to <inbox>+<token>@gmail.com,
  - and Connect Gmail (stylist_worker/gmail_sync.py), which reads the user's own
    inbox and calls accept_for_owner directly.

WHAT IS ACCEPTED
----------------
- From a known store (stylist_shop.order_email.KNOWN_STORES): a Gmail filter
  auto-forwarding keeps the store as the sender.
- From the user's OWN account email: a manual "Forward", where the store is
  then read from the forwarded block in the body.
- Gmail's forwarding-confirmation email: stored so its code can be shown in the
  app, because without it the user can never finish setting up the filter.
Anything else is recorded as `ignored`, so the user can see why a forwarded
email did nothing, instead of guessing.

Reading the email (a model call) and fetching photos happen later, in
stylist_worker/purchases.py, off the back of the outbox event emitted here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from typing import Any

from sqlalchemy import text

from stylist_api.settings import Settings
from stylist_db.outbox import emit
from stylist_db.session import system_session, tenant_session
from stylist_shop.order_email import (
    addresses,
    gmail_confirmation,
    inbox_address,
    inbox_token,
    read_email,
    store_for,
)

logger = logging.getLogger(__name__)

# An order email is tens of KB; a megabyte is already generous.
MAX_BODY_CHARS = 1_000_000


def enabled(settings: Settings) -> bool:
    return bool(settings.inbound_gmail_address or settings.inbound_email_domain)


def address_for(token: str, settings: Settings) -> str | None:
    return inbox_address(
        token,
        domain=settings.inbound_email_domain,
        gmail_address=settings.inbound_gmail_address,
    )


def message_id_from(headers: str, sender: str, subject: str, body: str) -> str:
    match = re.search(r"^Message-ID:\s*(<[^>]+>)", headers or "", re.IGNORECASE | re.MULTILINE)
    if match:
        return match.group(1)[:512]
    # No Message-ID: derive a stable one, so a redelivery still dedupes.
    return "sha256:" + hashlib.sha256(f"{sender}|{subject}|{body}".encode()).hexdigest()


async def accept_email(
    *,
    recipients: list[str],
    sender_header: str,
    subject: str,
    html: str | None,
    plain: str | None,
    headers: str,
    settings: Settings,
) -> dict[str, Any]:
    """Store one email for its owner. Idempotent per (owner, Message-ID)."""
    token = inbox_token(
        recipients,
        domain=settings.inbound_email_domain,
        gmail_address=settings.inbound_gmail_address,
    )
    if token is None:
        return {"accepted": False, "reason": "no inbox address"}

    async with system_session() as sys_db:
        owner = (
            await sys_db.execute(text("SELECT purchase_inbox_owner(:t)"), {"t": token})
        ).scalar()
    if owner is None:
        return {"accepted": False, "reason": "unknown inbox"}
    return await accept_for_owner(
        owner,
        sender_header=sender_header,
        subject=subject,
        html=html,
        plain=plain,
        headers=headers,
    )


async def accept_for_owner(
    owner: uuid.UUID,
    *,
    sender_header: str,
    subject: str,
    html: str | None,
    plain: str | None,
    headers: str,
) -> dict[str, Any]:
    """Store one email for a KNOWN owner. Connect Gmail calls this directly:
    the mail came out of that user's own inbox, so there is no address to read
    the owner from. The sender rules are identical either way."""
    sender = (addresses(sender_header) or [""])[0]
    subject = subject[:998]
    html = (html or "")[:MAX_BODY_CHARS] or None
    plain = (plain or "")[:MAX_BODY_CHARS] or None
    message_id = message_id_from(headers, sender, subject, plain or html or "")

    async with tenant_session(owner) as db:
        account_email = (
            await db.execute(text("SELECT email FROM users WHERE id = :u"), {"u": owner})
        ).scalar()

        body_text, _ = read_email(html, plain)
        confirmation = gmail_confirmation(sender, body_text)
        store = store_for(sender)
        if store is None and sender and sender == (account_email or "").lower():
            # A manual forward: the store is whoever sent the forwarded message.
            store = next((s for a in addresses(body_text) if (s := store_for(a)) is not None), None)

        email_id = uuid.uuid4()
        detail: dict[str, Any]
        if confirmation is not None:
            state = "gmail_confirmation"
            detail = {"code": confirmation.code, "link": confirmation.link}
            html = plain = None
        elif store is None:
            state = "ignored"
            detail = {"reason": f"{sender or 'unknown sender'} is not a store we read orders from"}
            html = plain = None
        else:
            state, detail = "received", {}

        inserted = (
            await db.execute(
                text(
                    """
                    INSERT INTO purchase_email
                        (id, user_id, message_id, sender, subject, store, status, detail,
                         body_html, body_text)
                    VALUES (:id, :u, :mid, :sender, :subject, :store, :status,
                            CAST(:detail AS jsonb), :html, :plain)
                    ON CONFLICT (user_id, message_id) DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "id": email_id,
                    "u": str(owner),
                    "mid": message_id,
                    "sender": sender[:320],
                    "subject": subject,
                    "store": store,
                    "status": state,
                    "detail": json.dumps(detail),
                    "html": html,
                    "plain": plain,
                },
            )
        ).scalar()
        if inserted is None:
            return {"accepted": True, "duplicate": True}
        if state == "received":
            await emit(
                db,
                aggregate_id=email_id,
                user_id=owner,
                event_type="purchase_email.received",
                payload={"store": store},
            )

    logger.info("purchase email %s for %s: %s", email_id, owner, state)
    return {"accepted": True, "status": state}
