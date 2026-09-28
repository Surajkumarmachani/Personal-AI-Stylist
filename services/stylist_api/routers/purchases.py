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
It reads the form and hands off to stylist_api.purchase_intake.accept_email,
which the Gmail inbox poller also uses: owner lookup, sender rules and storage
live there, once. Reading the email (a model call) and fetching photos happen
in the worker, so a slow store CDN or model can never make the provider time
out and resend.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import text

from stylist_api.deps import CurrentUser, SettingsDep, TenantDB
from stylist_api.purchase_intake import accept_email, address_for, enabled
from stylist_shop.order_email import addresses

logger = logging.getLogger(__name__)
router = APIRouter(tags=["purchases"])

RECENT_LIMIT = 20

# The filter the app tells the user to create in Gmail.
GMAIL_FILTER_QUERY = (
    "from:(myntra.com OR ajio.com OR amazon.in OR flipkart.com OR nykaafashion.com "
    "OR tatacliq.com OR meesho.com) subject:(order OR shipped OR delivered)"
)


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
    if not enabled(settings):
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
        "address": address_for(token, settings),
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
    if not enabled(settings):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not enabled")
    token = secrets.token_hex(8)
    await db.execute(
        text(
            "INSERT INTO purchase_inbox (user_id, token) VALUES (:u, :t) "
            "ON CONFLICT (user_id) DO UPDATE SET token = EXCLUDED.token, created_at = now()"
        ),
        {"u": str(user.id), "t": token},
    )
    return {"address": address_for(token, settings)}


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
    # Always 200 from here on, even for an unknown inbox: a non-2xx makes the
    # provider retry an email that can never be delivered, forever.
    return await accept_email(
        recipients=recipients,
        sender_header=field("from"),
        subject=field("subject"),
        html=field("html"),
        plain=field("text"),
        headers=field("headers"),
        settings=settings,
    )
