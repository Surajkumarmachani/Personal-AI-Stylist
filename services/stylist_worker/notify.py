"""The 07:00 local daily digest (Phase 8, `w-notify`).

"07:00 LOCAL" IS THE WHOLE DESIGN PROBLEM
------------------------------------------
There is no single moment to send. 07:00 exists 24+ times a day across zones,
so the job runs EVERY HOUR and each run sends only to users for whom it is
currently 07:00 in their own timezone. A daily cron at a fixed UTC hour would
notify everyone at 07:00 in one arbitrary place, which is the middle of the
night for most of them.

The timezone comes from the DEVICE where one is registered, falling back to
`user_profile.timezone`. Local is a property of where the phone is: a user who
flies to London should get their digest at 07:00 there, not at 07:00 in the
zone they signed up in.

SENDING TWICE IS UNFORGIVABLE, SO THE GUARD IS IN THE DATABASE
---------------------------------------------------------------
`push_send` has a unique index on `(user_id, kind, sent_on)` and `sent_on` is
the user's LOCAL date. The row is inserted BEFORE the send: a crash after
insert means one missed digest, a crash after send but before insert means a
second notification tomorrow morning at 07:00 plus one now. The first failure
is invisible; the second gets the app uninstalled.

WHAT THE NOTIFICATION SAYS
--------------------------
The top suggestion's garments, and nothing about how it was ranked. A push is
one line on a lock screen — it is an invitation to open the app, not a place to
explain a scorer. It carries the outfit hash as data so the app can deep-link
straight to that board rather than dumping the user on a home screen.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
import zoneinfo
from typing import Any

from sqlalchemy import text

from stylist_api.settings import get_settings
from stylist_clients.fcm import FCMClient, PushUnavailable, ServiceAccount
from stylist_db.session import system_session, tenant_session
from stylist_obs import stage_span

logger = logging.getLogger(__name__)

DIGEST_KIND = "daily_digest"
# The plan's "07:00 local". The job runs hourly and only fires for tenants
# whose local hour matches.
DIGEST_LOCAL_HOUR = 7

_client: FCMClient | None = None


def get_client() -> FCMClient | None:
    """The FCM client, or None when push is not configured.

    Unconfigured is a normal state, not an error: the stack must come up and
    every other feature must work without a Firebase credential, exactly as it
    does without a provider key.
    """
    global _client
    if _client is not None:
        return _client
    path = get_settings().firebase_credentials_file
    if not path:
        return None
    try:
        _client = FCMClient(ServiceAccount.from_file(path))
    except Exception as exc:
        # A misconfigured credential must not crash the worker on every tick.
        logger.warning("push disabled: cannot load %s: %s", path, exc)
        return None
    return _client


def reset() -> None:
    """For tests, so a fake does not leak between cases."""
    global _client
    _client = None


def _local_now(timezone: str | None) -> dt.datetime:
    try:
        return dt.datetime.now(zoneinfo.ZoneInfo(timezone or "Asia/Kolkata"))
    except Exception:
        # An unknown zone string (a typo, or a device sending something
        # non-IANA) must not stop the whole run. Falling back is better than
        # skipping the user forever.
        return dt.datetime.now(zoneinfo.ZoneInfo("Asia/Kolkata"))


async def _tenants(session: Any) -> list[uuid.UUID]:
    rows = await session.execute(text("SELECT user_id FROM push_tenants()"))
    return [r[0] for r in rows]


async def send_digest_for_tenant(
    user_id: uuid.UUID, *, now: dt.datetime | None = None, force: bool = False
) -> dict[str, Any]:
    """Send today's digest to one tenant, if it is 07:00 where they are."""
    client = get_client()
    if client is None:
        return {"sent": False, "reason": "push_not_configured"}

    async with tenant_session(user_id) as db:
        devices = await db.execute(
            text(
                "SELECT id, token, timezone FROM device_token "
                "WHERE disabled_at IS NULL ORDER BY created_at"
            )
        )
        rows = [dict(r) for r in devices.mappings()]
        if not rows:
            return {"sent": False, "reason": "no_devices"}

        profile = await db.execute(text("SELECT timezone FROM user_profile LIMIT 1"))
        default_tz = profile.scalar() or "Asia/Kolkata"
        tz = rows[0]["timezone"] or default_tz
        local = now or _local_now(tz)

        if not force and local.hour != DIGEST_LOCAL_HOUR:
            return {"sent": False, "reason": "not_local_07", "local_hour": local.hour}

        # THE GUARD, BEFORE THE SEND. A crash after this insert costs one
        # missed digest; a crash after sending but before recording costs a
        # duplicate tomorrow. Only one of those gets the app uninstalled.
        claimed = await db.execute(
            text(
                "INSERT INTO push_send (id, user_id, kind, sent_on, devices_targeted) "
                "VALUES (:id, :uid, :kind, :day, :n) "
                "ON CONFLICT (user_id, kind, sent_on) DO NOTHING RETURNING id"
            ),
            {
                "id": uuid.uuid4(),
                "uid": user_id,
                "kind": DIGEST_KIND,
                "day": local.date(),
                "n": len(rows),
            },
        )
        if claimed.scalar_one_or_none() is None:
            return {"sent": False, "reason": "already_sent_today"}

        outfit = await db.execute(
            text(
                """
                SELECT garment_set_hash, garment_ids
                FROM outfits
                ORDER BY score DESC, garment_set_hash
                LIMIT 1
                """
            )
        )
        top = outfit.mappings().one_or_none()
        if top is None:
            # The claim row stays. A user with nothing to suggest should not be
            # retried every hour for the rest of the day.
            return {"sent": False, "reason": "no_outfits"}

        names = await db.execute(
            text(
                "SELECT subcategory::text AS s FROM garments "
                "WHERE id = ANY(CAST(:ids AS uuid[])) AND is_active LIMIT 4"
            ),
            {"ids": [str(g) for g in top["garment_ids"]]},
        )
        pieces = [r["s"].replace("_", " ") for r in names.mappings() if r["s"]]

    body = ", ".join(pieces[:3]) if pieces else "Your outfit for today is ready"
    try:
        result = await client.send(
            [r["token"] for r in rows],
            title="Today's outfit",
            body=body,
            # Deep-link data, so tapping opens THIS board rather than a home
            # screen the user then has to navigate from.
            data={"kind": DIGEST_KIND, "garment_set_hash": str(top["garment_set_hash"])},
        )
    except PushUnavailable as exc:
        logger.info("push unavailable for %s: %s", user_id, exc)
        return {"sent": False, "reason": "unavailable", "detail": str(exc)}

    async with tenant_session(user_id) as db:
        for token, reason in result.permanent_failure:
            # FCM told us this device is gone. Disabled, not deleted — "why did
            # my notifications stop" needs an answer, and a deleted row has
            # none.
            await db.execute(
                text(
                    "UPDATE device_token SET disabled_at = now(), disabled_reason = :r "
                    "WHERE token = :t"
                ),
                {"t": token, "r": reason[:64]},
            )
        if result.delivered:
            await db.execute(
                text(
                    "UPDATE device_token SET last_success_at = now(), failure_count = 0 "
                    "WHERE token = ANY(:tokens)"
                ),
                {"tokens": list(result.delivered)},
            )
        for token in result.transient_failure:
            await db.execute(
                text("UPDATE device_token SET failure_count = failure_count + 1 WHERE token = :t"),
                {"t": token},
            )

    return {
        "sent": result.ok > 0,
        "delivered": result.ok,
        "disabled": len(result.permanent_failure),
        "transient": len(result.transient_failure),
    }


async def hourly_digest(ctx: dict[str, Any]) -> dict[str, Any]:
    """Cron entry point. Runs EVERY HOUR; each tenant fires at their own 07:00.

    Not guarded by an advisory lock the way the nightly precompute is: the
    `push_send` unique index already makes a duplicate run a no-op, and it does
    so per tenant rather than per job — which is strictly stronger, because a
    lock would also have to be held for the whole run.
    """
    if get_client() is None:
        return {"ran": False, "reason": "push_not_configured"}

    async with system_session() as session:
        tenants = await _tenants(session)

    sent = skipped = 0
    with stage_span("hourly_digest"):
        for user_id in tenants:
            try:
                result = await send_digest_for_tenant(user_id)
            except Exception as exc:
                # One tenant's failure must not abandon the rest. A push job
                # that stops at the first bad token silently drops everyone
                # after it in the list.
                logger.warning("digest failed for %s: %s", user_id, exc)
                skipped += 1
                continue
            if result.get("sent"):
                sent += 1
            else:
                skipped += 1

    logger.info("hourly_digest tenants=%d sent=%d skipped=%d", len(tenants), sent, skipped)
    return {"ran": True, "tenants": len(tenants), "sent": sent, "skipped": skipped}
