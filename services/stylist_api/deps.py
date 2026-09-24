"""Request-scoped dependencies.

`current_user` resolves the tenant, and `tenant_db` opens a transaction already
bound to that tenant. Route handlers should take `tenant_db` and never build a
session themselves — that is what keeps "forgot the WHERE clause" from being a
data leak.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, cast

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from stylist_api.settings import Settings, get_settings
from stylist_clients.litellm_client import LiteLLMClient
from stylist_clients.redis_client import CacheRedis, QueueRedis
from stylist_clients.storage import ObjectStore
from stylist_db.models import User
from stylist_db.session import system_session, tenant_session

bearer = HTTPBearer(auto_error=False)
# Partner servers (migration 0027). Declared as security schemes so /docs shows
# them and "Authorize" works with a key as well as with a bearer token.
api_key_header = APIKeyHeader(
    name="X-API-Key",
    scheme_name="PartnerKey",
    auto_error=False,
    description="Partner API key (sty_…).",
)
partner_user_header = APIKeyHeader(
    name="X-User-Id",
    scheme_name="PartnerUser",
    auto_error=False,
    description="With X-API-Key: YOUR id for the user this call is for.",
)


@dataclass(frozen=True, slots=True)
class ApiClient:
    id: uuid.UUID
    name: str


def settings_dep() -> Settings:
    return get_settings()


def queue_redis(request: Request) -> QueueRedis:
    return cast(QueueRedis, request.app.state.queue_redis)


def cache_redis(request: Request) -> CacheRedis:
    return cast(CacheRedis, request.app.state.cache_redis)


def litellm(request: Request) -> LiteLLMClient:
    return cast(LiteLLMClient, request.app.state.litellm)


def object_store(request: Request) -> ObjectStore:
    return cast(ObjectStore, request.app.state.object_store)


async def _resolve_api_key(request: Request, key: str) -> ApiClient:
    """The partner a key belongs to, after the rate limit. 401/429 otherwise.

    One indexed lookup by prefix, then a constant-time hash compare. Every
    failure is the same 401 so a caller cannot tell a revoked key from an
    unknown one.
    """
    from stylist_api.api_keys import matches, parse_prefix

    denied = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")
    prefix = parse_prefix(key)
    if prefix is None:
        raise denied
    async with system_session() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT k.id AS key_id, k.key_hash, k.last_used_at,
                           c.id AS client_id, c.name, c.rate_limit_per_minute
                    FROM api_key k JOIN api_client c ON c.id = k.client_id
                    WHERE k.prefix = :p AND k.revoked_at IS NULL AND c.disabled_at IS NULL
                    """
                ),
                {"p": prefix},
            )
        ).mappings().one_or_none()
        if row is None or not matches(key, row["key_hash"]):
            raise denied
        # At most one write a minute per key: `last_used_at` is for "is this
        # key still in use?", not an access log, and a write per request
        # would double the cost of every partner call.
        await session.execute(
            text(
                "UPDATE api_key SET last_used_at = now() WHERE id = :id "
                "AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')"
            ),
            {"id": row["key_id"]},
        )

    # Per CLIENT, not per key: issuing a second key must not double a
    # partner's allowance. Fixed one-minute windows.
    window = int(time.time() // 60)
    cache = cast(CacheRedis, request.app.state.cache_redis)
    try:
        count = await cache.incr_window(f"ratelimit:{row['client_id']}:{window}", ttl_seconds=120)
    except Exception:
        # A cache outage must not take every partner down with it. The limit
        # is fairness, not security — the key check above already ran.
        count = 0
    if count > row["rate_limit_per_minute"]:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"rate limit is {row['rate_limit_per_minute']} requests per minute",
            headers={"Retry-After": str(60 - int(time.time()) % 60)},
        )
    return ApiClient(id=row["client_id"], name=row["name"])


async def current_api_client(
    request: Request,
    api_key: Annotated[str | None, Depends(api_key_header)],
) -> ApiClient:
    """For `/partner/*` routes, which act for the partner itself, not a user."""
    if not api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing X-API-Key")
    return await _resolve_api_key(request, api_key)


async def current_user_id(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    settings: Annotated[Settings, Depends(settings_dep)],
    api_key: Annotated[str | None, Depends(api_key_header)],
    external_id: Annotated[str | None, Depends(partner_user_header)],
) -> uuid.UUID:
    # PARTNER PATH. The key identifies the partner; `X-User-Id` names one of
    # ITS users. The lookup is scoped by `api_client_id`, so a partner can
    # only ever reach accounts it created — never a directly-registered user,
    # and never another partner's. See migration 0027.
    if api_key:
        client = await _resolve_api_key(request, api_key)
        if not external_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-User-Id is required with X-API-Key",
            )
        async with system_session() as session:
            uid = (
                await session.execute(
                    select(User.id).where(
                        User.api_client_id == client.id, User.external_id == external_id
                    )
                )
            ).scalar_one_or_none()
        if uid is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="unknown user; create it with POST /partner/users",
            )
        return uid

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    from stylist_api.security import decode_token

    try:
        claims = decode_token(
            credentials.credentials,
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
            expect="access",
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return claims.user_id


async def current_user(
    user_id: Annotated[uuid.UUID, Depends(current_user_id)],
) -> User:
    """Load the user and enforce the erasure contract.

    A soft-deleted account returns 410 Gone from the moment step 1 of the saga
    runs, which can be up to 30 days before the rows are actually deleted. It
    must NOT be a 404 or a 401 — the account existed, it was deleted, and the
    user is entitled to an unambiguous answer.
    """
    async with system_session() as session:
        row = await session.execute(select(User).where(User.id == user_id))
        user = row.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unknown user")
    if user.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="account deleted")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account disabled")
    return user


async def tenant_db(
    user: Annotated[User, Depends(current_user)],
) -> AsyncIterator[AsyncSession]:
    """A transaction bound to the caller's tenant for its whole lifetime."""
    async with tenant_session(user.id) as session:
        yield session


async def current_admin(
    user: Annotated[User, Depends(current_user)],
) -> User:
    """A user with `is_admin`. 403 otherwise.

    GUARDS CROSS-TENANT DATA, not a nicer screen. `/ops` serves aggregates
    that deliberately read past RLS through SECURITY DEFINER functions —
    ingest funnels, latency percentiles, DLQ depth, correction rates, model
    spend — for the WHOLE deployment. `routers/ops.py` has said since Phase 5
    that it "belongs behind an admin authorisation boundary rather than a user
    token"; until this existed, any registered account could read all of it.

    404 would be the usual choice for hiding a resource's existence, but these
    paths are in the public OpenAPI document and pretending otherwise would be
    theatre. 403 is the honest answer: the route exists, you may not have it.
    """
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin only",
        )
    return user


CurrentUser = Annotated[User, Depends(current_user)]
CurrentAdmin = Annotated[User, Depends(current_admin)]
CurrentApiClient = Annotated[ApiClient, Depends(current_api_client)]
TenantDB = Annotated[AsyncSession, Depends(tenant_db)]
SettingsDep = Annotated[Settings, Depends(settings_dep)]
QueueRedisDep = Annotated[QueueRedis, Depends(queue_redis)]
CacheRedisDep = Annotated[CacheRedis, Depends(cache_redis)]
ObjectStoreDep = Annotated[ObjectStore, Depends(object_store)]
LiteLLMDep = Annotated[LiteLLMClient, Depends(litellm)]
