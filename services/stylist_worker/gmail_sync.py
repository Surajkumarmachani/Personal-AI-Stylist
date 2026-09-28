"""Connect Gmail: pull new store order emails from each linked inbox (Phase 15).

Cron, every 15 minutes. For every user who pressed "Connect Gmail":
refresh the access token, search their inbox for order emails from known stores
newer than the last sync, and hand each one to the same intake forwarding uses
(stylist_api.purchase_intake.accept_for_owner). The worker's
`ingest_order_email` then reads them exactly as it reads forwarded mail.

WHAT IS READ
------------
Only messages matching `google_gmail.order_query`: from a known store domain,
with an order-ish subject, outside Promotions. Nothing else in the inbox is
fetched. The first sync looks back FIRST_SYNC_DAYS so a user who connects after
shopping still gets their recent orders.

A DEAD GRANT IS A STATE, NOT A RETRY LOOP
-----------------------------------------
Revoked at Google, the password changed, or a Testing-mode token past its
7 days: the link is marked revoked (token cleared) and the Profile page shows
"reconnect". Retrying every 15 minutes could not fix any of those.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from stylist_api.purchase_intake import accept_for_owner
from stylist_api.settings import get_settings
from stylist_clients import google_calendar as gcal
from stylist_clients import google_gmail
from stylist_db.session import system_session, tenant_session
from stylist_shop.order_email import KNOWN_STORES
from stylist_worker.inbox_poll import fields_from_message

logger = logging.getLogger(__name__)

PROVIDER = "google_gmail"
FIRST_SYNC_DAYS = 30
PER_RUN_LIMIT = 20


async def sync_one(user_id: uuid.UUID) -> dict[str, Any]:
    settings = get_settings()
    async with tenant_session(user_id) as db:
        row = (
            (
                await db.execute(
                    text(
                        "SELECT refresh_token, last_synced_at FROM calendar_link "
                        "WHERE provider = :p AND refresh_token IS NOT NULL AND revoked_at IS NULL"
                    ),
                    {"p": PROVIDER},
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        return {"skipped": "not linked"}

    started = datetime.now(UTC)
    since = row["last_synced_at"] or (started - timedelta(days=FIRST_SYNC_DAYS))
    # A small overlap: Gmail's `after:` is second-granular and delivery is not
    # instant. A message seen twice is deduped on its Message-ID by the intake.
    after_epoch = int((since - timedelta(minutes=5)).timestamp())

    try:
        grant = await gcal.refresh_access_token(
            row["refresh_token"],
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
        )
        ids = await google_gmail.list_message_ids(
            grant.access_token,
            query=google_gmail.order_query(sorted(KNOWN_STORES), after_epoch=after_epoch),
            limit=PER_RUN_LIMIT,
        )
        outcomes: dict[str, int] = {}
        for message_id in ids:
            raw = await google_gmail.get_raw(grant.access_token, message_id)
            fields = fields_from_message(raw)
            fields.pop("recipients")
            result = await accept_for_owner(user_id, **fields)
            key = str(result.get("status") or ("duplicate" if result.get("duplicate") else "?"))
            outcomes[key] = outcomes.get(key, 0) + 1
    except gcal.CalendarReauthRequired:
        async with tenant_session(user_id) as db:
            await db.execute(
                text(
                    "UPDATE calendar_link SET refresh_token = NULL, revoked_at = now() "
                    "WHERE provider = :p"
                ),
                {"p": PROVIDER},
            )
        logger.info("gmail grant for %s is no longer valid; marked for reconnect", user_id)
        return {"reconnect_required": True}

    async with tenant_session(user_id) as db:
        await db.execute(
            text("UPDATE calendar_link SET last_synced_at = :t WHERE provider = :p"),
            {"t": started, "p": PROVIDER},
        )
    return {"found": len(ids), **outcomes}


async def sync_gmail_orders(ctx: dict[str, Any]) -> dict[str, Any]:
    """One run over every linked inbox. One tenant's failure never stops the rest."""
    settings = get_settings()
    if not (settings.google_client_id and settings.google_client_secret):
        return {"skipped": "google oauth not configured"}

    async with system_session() as session:
        tenants = [r[0] for r in await session.execute(text("SELECT user_id FROM gmail_tenants()"))]

    synced = failed = 0
    for user_id in tenants:
        try:
            await sync_one(user_id)
            synced += 1
        except Exception as exc:
            logger.warning("gmail sync failed for %s: %s", user_id, exc)
            failed += 1
    return {"tenants": len(tenants), "synced": synced, "failed": failed}
