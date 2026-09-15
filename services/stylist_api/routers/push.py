"""Device registration for push (Phase 8).

REGISTRATION IS AN UPSERT, BECAUSE THE APP CALLS IT ON EVERY LAUNCH
--------------------------------------------------------------------
FCM hands the app a registration token at startup and it can change at any
time — reinstall, restore from backup, token rotation. The client's correct
behaviour is to POST it every launch, so this endpoint has to be idempotent or
the table fills with duplicates of the same device and the user gets one
notification per launch they have ever made.

Re-registering also REVIVES a disabled row. A token FCM previously rejected can
become live again when the app is reinstalled, and refusing to reuse it would
leave the user permanently unnotifiable with no way to fix it from the app.

UNSUBSCRIBING IS A FIRST-CLASS ACTION
--------------------------------------
`DELETE /push/devices/{id}` disables one device; `DELETE /push/devices` turns
push off everywhere. Both keep the row — "why did notifications stop" is a
question someone will ask, and a deleted row cannot answer it.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from stylist_api.deps import CurrentUser, TenantDB
from stylist_db.session import tenant_session

router = APIRouter(tags=["push"])


class DeviceRequest(BaseModel):
    # FCM tokens are long and the length varies by platform; capped only to
    # stop an unbounded body, not to validate a format we do not control.
    token: str = Field(min_length=16, max_length=4096)
    platform: Literal["ios", "android", "web"]
    # IANA zone for THIS device. "07:00 local" is a property of where the phone
    # is, not of the account — a user who travels should get their digest at
    # 07:00 where they are.
    timezone: str | None = Field(default=None, max_length=64)


@router.post("/push/devices", status_code=status.HTTP_201_CREATED)
async def register_device(body: DeviceRequest, user: CurrentUser) -> dict[str, Any]:
    """Register (or refresh) this device. Idempotent."""
    async with tenant_session(user.id) as db:
        row = await db.execute(
            text(
                """
                INSERT INTO device_token (id, user_id, token, platform, timezone, last_seen_at)
                VALUES (:id, :uid, :token, :platform, :tz, now())
                ON CONFLICT (user_id, token) DO UPDATE SET
                    platform = EXCLUDED.platform,
                    timezone = COALESCE(EXCLUDED.timezone, device_token.timezone),
                    last_seen_at = now(),
                    -- Reviving a previously-dead token. A reinstall makes an
                    -- UNREGISTERED token live again, and refusing to reuse it
                    -- would leave the user unnotifiable with no fix from the
                    -- app.
                    disabled_at = NULL,
                    disabled_reason = NULL,
                    failure_count = 0
                RETURNING id
                """
            ),
            {
                "id": uuid.uuid4(),
                "uid": user.id,
                "token": body.token,
                "platform": body.platform,
                "tz": body.timezone,
            },
        )
        return {"device_id": str(row.scalar_one()), "registered": True}


@router.get("/push/devices")
async def list_devices(user: CurrentUser, db: TenantDB) -> dict[str, Any]:
    """The user's devices. The TOKEN IS NEVER RETURNED — it identifies a device
    and there is nothing a client can do with its own token that it does not
    already know."""
    rows = await db.execute(
        text(
            "SELECT id, platform, timezone, created_at, last_seen_at, last_success_at, "
            "failure_count, disabled_at, disabled_reason FROM device_token "
            "ORDER BY created_at DESC"
        )
    )
    return {"devices": [dict(r) for r in rows.mappings()]}


@router.delete("/push/devices/{device_id}", status_code=status.HTTP_200_OK)
async def disable_device(device_id: uuid.UUID, user: CurrentUser) -> dict[str, Any]:
    async with tenant_session(user.id) as db:
        row = await db.execute(
            text(
                "UPDATE device_token SET disabled_at = now(), disabled_reason = 'user_disabled' "
                "WHERE id = :id AND disabled_at IS NULL RETURNING id"
            ),
            {"id": device_id},
        )
        if row.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="device not found or already off"
            )
    return {"disabled": True}


@router.delete("/push/devices", status_code=status.HTTP_200_OK)
async def disable_all(user: CurrentUser) -> dict[str, Any]:
    """Turn push off entirely. One action, because a user who wants the
    notifications to stop should not have to find every device they have ever
    signed in on."""
    async with tenant_session(user.id) as db:
        row = await db.execute(
            text(
                "UPDATE device_token SET disabled_at = now(), disabled_reason = 'user_disabled' "
                "WHERE disabled_at IS NULL RETURNING id"
            )
        )
        return {"disabled": len(list(row))}
