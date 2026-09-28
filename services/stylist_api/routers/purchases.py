"""Purchases from forwarded order emails (Phase 15).

    GET  /me/purchase-inbox          the user's forwarding address + recent emails
    POST /me/purchase-inbox/rotate   retire the address and issue a new one
    POST /inbound/email?key=...      the mail provider's webhook: multipart
                                     form fields in SendGrid Inbound Parse's
                                     shape, which is also what the free
                                     Cloudflare Email Worker in
                                     infra/cloudflare/email-worker posts

THE WEBHOOK DOES AS LITTLE AS POSSIBLE
--------------------------------------
It finds the owner from the recipient address, stores the email, and emits an
outbox event in the same transaction. Reading the email (a model call) and
fetching photos happen in the worker (stylist_worker/purchases.py), so a slow
store CDN or model can never make the provider time out and resend.

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
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import text

from stylist_api.deps import CurrentUser, SettingsDep, TenantDB
from stylist_db.outbox import emit
from stylist_db.session import system_session, tenant_session
from stylist_shop.order_email import (
    addresses,
    gmail_confirmation,
    inbox_token,
    read_email,
    store_for,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["purchases"])

# An order email is tens of KB; a megabyte is already generous.
MAX_BODY_CHARS = 1_000_000
RECENT_LIMIT = 20

# The filter the app tells the user to create in Gmail.
GMAIL_FILTER_QUERY = (
    "from:(myntra.com OR ajio.com OR amazon.in OR flipkart.com OR nykaafashion.com "
    "OR tatacliq.com OR meesho.com) subject:(order OR shipped OR delivered)"
)


def _address(token: str, domain: str) -> str:
    return f"orders-{token}@{domain}"


async def _ensure_token(db: Any, user_id: uuid.UUID) -> str:
    token = (await db.execute(text("SELECT token FROM purchase_inbox"))).scalar()
    if token:
        return str(token)
    token = secrets.token_hex(8)  # 16 chars of [0-9a-f], within the 12-32 the parser accepts
    await db.execute(
        text(
            "INSERT INTO purchase_inbox (user_id, token) VALUES (:u, :t) "
            "ON CONFLICT (user_id) DO NOTHING"
        ),
        {"u": str(user_id), "t": token},
    )
    return str((await db.execute(text("SELECT token FROM purchase_inbox"))).scalar())


@router.get("/me/purchase-inbox")
async def purchase_inbox(user: CurrentUser, db: TenantDB, settings: SettingsDep) -> dict[str, Any]:
    if not settings.inbound_email_domain:
        return {"enabled": False, "address": None, "recent": [], "gmail_confirmation": None}

    token = await _ensure_token(db, user.id)
    rows = await db.execute(
        text(
            "SELECT id, received_at, store, subject, status, detail FROM purchase_email "
            "ORDER BY received_at DESC LIMIT :n"
        ),
        {"n": RECENT_LIMIT},
    )
    recent = [
        {
            "id": str(r["id"]),
            "received_at": r["received_at"].isoformat(),
            "store": r["store"],
            "subject": r["subject"],
            "status": r["status"],
            "detail": r["detail"],
        }
        for r in rows.mappings()
    ]
    confirmation = next((r["detail"] for r in recent if r["status"] == "gmail_confirmation"), None)
    return {
        "enabled": True,
        "address": _address(token, settings.inbound_email_domain),
        "gmail_filter": GMAIL_FILTER_QUERY,
        "recent": recent,
        # Only while setup is unfinished: once a real order has arrived the
        # code has done its job and showing it again is noise.
        "gmail_confirmation": confirmation
        if confirmation and not any(r["status"] != "gmail_confirmation" for r in recent)
        else None,
    }


@router.post("/me/purchase-inbox/rotate")
async def rotate_inbox(user: CurrentUser, db: TenantDB, settings: SettingsDep) -> dict[str, Any]:
    if not settings.inbound_email_domain:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not enabled")
    token = secrets.token_hex(8)
    await db.execute(
        text(
            "INSERT INTO purchase_inbox (user_id, token) VALUES (:u, :t) "
            "ON CONFLICT (user_id) DO UPDATE SET token = EXCLUDED.token, created_at = now()"
        ),
        {"u": str(user.id), "t": token},
    )
    return {"address": _address(token, settings.inbound_email_domain)}


def _message_id(headers: str, sender: str, subject: str, body: str) -> str:
    match = re.search(r"^Message-ID:\s*(<[^>]+>)", headers or "", re.IGNORECASE | re.MULTILINE)
    if match:
        return match.group(1)[:512]
    # No Message-ID: derive a stable one, so a provider retry still dedupes.
    return "sha256:" + hashlib.sha256(f"{sender}|{subject}|{body}".encode()).hexdigest()


@router.post("/inbound/email", include_in_schema=False)
async def inbound_email(request: Request, settings: SettingsDep) -> dict[str, Any]:
    # CLOSED unless configured, and the key compared in constant time. 404 rather
    # than 401, so an unconfigured deployment does not advertise the endpoint.
    key = request.query_params.get("key", "")
    if not settings.inbound_email_secret or not hmac.compare_digest(
        key, settings.inbound_email_secret
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    form = await request.form()

    def field(name: str) -> str:
        value = form.get(name)
        # Attachments arrive as UploadFile; only the text fields are read.
        return value if isinstance(value, str) else ""

    try:
        envelope = json.loads(field("envelope") or "{}")
    except json.JSONDecodeError:
        envelope = {}
    recipients = addresses(" ".join(envelope.get("to") or [])) + addresses(field("to"))
    token = inbox_token(recipients, settings.inbound_email_domain)
    if token is None:
        return {"accepted": False, "reason": "no inbox address"}

    async with system_session() as sys_db:
        owner = (
            await sys_db.execute(text("SELECT purchase_inbox_owner(:t)"), {"t": token})
        ).scalar()
    if owner is None:
        # Always 200: a non-2xx makes the provider retry an email that can
        # never be delivered, forever.
        return {"accepted": False, "reason": "unknown inbox"}

    sender = (addresses(field("from")) or [""])[0]
    subject = field("subject")[:998]
    html = field("html")[:MAX_BODY_CHARS] or None
    plain = field("text")[:MAX_BODY_CHARS] or None
    message_id = _message_id(field("headers"), sender, subject, plain or html or "")

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
        if confirmation is not None:
            state, detail = (
                "gmail_confirmation",
                {"code": confirmation.code, "link": confirmation.link},
            )
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
