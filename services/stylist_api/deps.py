"""Request-scoped dependencies.

`current_user` resolves the tenant, and `tenant_db` opens a transaction already
bound to that tenant. Route handlers should take `tenant_db` and never build a
session themselves — that is what keeps "forgot the WHERE clause" from being a
data leak.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated, cast

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stylist_api.settings import Settings, get_settings
from stylist_clients.redis_client import CacheRedis, QueueRedis
from stylist_clients.storage import ObjectStore
from stylist_db.models import User
from stylist_db.session import system_session, tenant_session

bearer = HTTPBearer(auto_error=False)


def settings_dep() -> Settings:
    return get_settings()


def queue_redis(request: Request) -> QueueRedis:
    return cast(QueueRedis, request.app.state.queue_redis)


def cache_redis(request: Request) -> CacheRedis:
    return cast(CacheRedis, request.app.state.cache_redis)


def object_store(request: Request) -> ObjectStore:
    return cast(ObjectStore, request.app.state.object_store)


async def current_user_id(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    settings: Annotated[Settings, Depends(settings_dep)],
) -> uuid.UUID:
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


CurrentUser = Annotated[User, Depends(current_user)]
TenantDB = Annotated[AsyncSession, Depends(tenant_db)]
SettingsDep = Annotated[Settings, Depends(settings_dep)]
QueueRedisDep = Annotated[QueueRedis, Depends(queue_redis)]
CacheRedisDep = Annotated[CacheRedis, Depends(cache_redis)]
ObjectStoreDep = Annotated[ObjectStore, Depends(object_store)]
