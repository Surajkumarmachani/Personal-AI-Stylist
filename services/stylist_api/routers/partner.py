"""Partner servers: API keys and the users they act for (migration 0027).

TWO HALVES
----------
`/admin/api-clients…` is how an ADMIN of this deployment creates a partner,
issues it keys and revokes them. `/partner/users` is how that PARTNER, holding
a key, creates the users it will act for. Everything else a partner does goes
through the ordinary endpoints with `X-API-Key` + `X-User-Id` — see
`deps.current_user_id` — so there is no second, partner-only copy of chat or
suggestions to keep in step with the first.

A KEY IS SHOWN ONCE
-------------------
Only its hash is stored. The issue response is the one and only time the
secret exists anywhere outside the partner's own systems; a lost key is
revoked and replaced, never recovered.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from stylist_api.api_keys import issue_key, partner_email
from stylist_api.deps import CurrentAdmin, CurrentApiClient, LiteLLMDep, SettingsDep
from stylist_api.routers.auth import create_account
from stylist_db.models import User
from stylist_db.session import system_session

router = APIRouter(tags=["partner"])

# A password hash that no password matches: `verify_password` raises on it and
# returns False. A partner's user never logs in; the partner acts for them.
NO_PASSWORD = "!"


# ------------------------------------------------------------------- admin


class ClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    rate_limit_per_minute: int = Field(default=120, ge=1, le=100_000)


class KeyCreate(BaseModel):
    label: str | None = Field(default=None, max_length=80)


@router.post("/admin/api-clients", status_code=status.HTTP_201_CREATED)
async def create_client(body: ClientCreate, admin: CurrentAdmin) -> dict[str, Any]:
    client_id = uuid.uuid4()
    async with system_session() as db:
        await db.execute(
            text(
                "INSERT INTO api_client (id, name, rate_limit_per_minute) "
                "VALUES (:id, :name, :rate)"
            ),
            {"id": client_id, "name": body.name, "rate": body.rate_limit_per_minute},
        )
    return {
        "id": str(client_id),
        "name": body.name,
        "rate_limit_per_minute": body.rate_limit_per_minute,
    }


@router.get("/admin/api-clients")
async def list_clients(admin: CurrentAdmin) -> dict[str, Any]:
    """Every client and its keys — prefixes only, never a secret or a hash."""
    async with system_session() as db:
        clients = (
            await db.execute(
                text(
                    "SELECT c.id, c.name, c.rate_limit_per_minute, c.created_at, c.disabled_at, "
                    "(SELECT count(*) FROM users u WHERE u.api_client_id = c.id) AS users "
                    "FROM api_client c ORDER BY c.created_at"
                )
            )
        ).mappings().all()
        keys = (
            await db.execute(
                text(
                    "SELECT id, client_id, prefix, label, created_at, last_used_at, revoked_at "
                    "FROM api_key ORDER BY created_at"
                )
            )
        ).mappings().all()

    def iso(v: Any) -> str | None:
        return v.isoformat() if v else None

    return {
        "clients": [
            {
                "id": str(c["id"]),
                "name": c["name"],
                "rate_limit_per_minute": c["rate_limit_per_minute"],
                "users": int(c["users"]),
                "created_at": iso(c["created_at"]),
                "disabled_at": iso(c["disabled_at"]),
                "keys": [
                    {
                        "id": str(k["id"]),
                        "prefix": f"sty_{k['prefix']}_…",
                        "label": k["label"],
                        "created_at": iso(k["created_at"]),
                        "last_used_at": iso(k["last_used_at"]),
                        "revoked_at": iso(k["revoked_at"]),
                    }
                    for k in keys
                    if k["client_id"] == c["id"]
                ],
            }
            for c in clients
        ]
    }


@router.post("/admin/api-clients/{client_id}/keys", status_code=status.HTTP_201_CREATED)
async def create_key(client_id: uuid.UUID, body: KeyCreate, admin: CurrentAdmin) -> dict[str, Any]:
    issued = issue_key()
    key_id = uuid.uuid4()
    async with system_session() as db:
        exists = (
            await db.execute(text("SELECT 1 FROM api_client WHERE id = :id"), {"id": client_id})
        ).scalar_one_or_none()
        if exists is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown client")
        await db.execute(
            text(
                "INSERT INTO api_key (id, client_id, prefix, key_hash, label) "
                "VALUES (:id, :cid, :prefix, :hash, :label)"
            ),
            {
                "id": key_id,
                "cid": client_id,
                "prefix": issued.prefix,
                "hash": issued.key_hash,
                "label": body.label,
            },
        )
    return {
        "id": str(key_id),
        # The only time this exists outside the partner's systems.
        "key": issued.key,
        "note": "Store this key now. It is not stored and cannot be shown again.",
    }


@router.delete("/admin/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(key_id: uuid.UUID, admin: CurrentAdmin) -> Response:
    """Immediate: the next request with this key is a 401."""
    async with system_session() as db:
        done = await db.execute(
            text(
                "UPDATE api_key SET revoked_at = now() WHERE id = :id AND revoked_at IS NULL "
                "RETURNING id"
            ),
            {"id": key_id},
        )
        if done.scalar_one_or_none() is None:
            exists = (
                await db.execute(text("SELECT 1 FROM api_key WHERE id = :id"), {"id": key_id})
            ).scalar_one_or_none()
            if exists is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown key")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------- partner


class PartnerUserCreate(BaseModel):
    # The partner's own id. Opaque to us; unique per partner.
    external_id: str = Field(min_length=1, max_length=128)
    dresses_as: Literal["women", "men", "all"] | None = None


@router.post("/partner/users")
async def create_partner_user(
    body: PartnerUserCreate,
    response: Response,
    client: CurrentApiClient,
    settings: SettingsDep,
    gateway: LiteLLMDep,
) -> dict[str, Any]:
    """Create (or find) one of this partner's users. Idempotent on external_id.

    201 when created, 200 when it already existed — so a partner can call it
    before every session without tracking whether it has before.
    """
    async with system_session() as db:
        existing = (
            await db.execute(
                select(User.id).where(
                    User.api_client_id == client.id, User.external_id == body.external_id
                )
            )
        ).scalar_one_or_none()
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return {"external_id": body.external_id, "created": False}

    try:
        await create_account(
            email=partner_email(str(client.id), body.external_id),
            password_hash=NO_PASSWORD,
            dresses_as=body.dresses_as,
            settings=settings,
            gateway=gateway,
            api_client_id=client.id,
            external_id=body.external_id,
        )
    except HTTPException as exc:
        # Two concurrent creates for the same id: the loser hits the unique
        # email and gets 409 from `create_account`. It exists now, which is
        # what the caller wanted.
        if exc.status_code != status.HTTP_409_CONFLICT:
            raise
        response.status_code = status.HTTP_200_OK
        return {"external_id": body.external_id, "created": False}
    response.status_code = status.HTTP_201_CREATED
    return {"external_id": body.external_id, "created": True}
