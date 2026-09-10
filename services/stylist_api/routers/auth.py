"""Registration, login, refresh, logout."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import jwt
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from stylist_api.deps import LiteLLMDep, QueueRedisDep, SettingsDep
from stylist_api.schemas import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
)
from stylist_api.security import (
    decode_token,
    hash_password,
    issue_token_pair,
    verify_password,
)
from stylist_db.models import User, UserProfile
from stylist_db.session import system_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    settings: SettingsDep,
    gateway: LiteLLMDep,
) -> TokenResponse:
    user_id = uuid.uuid4()
    # users/user_profile creation runs without tenant context: the tenant does
    # not exist yet, so there is nothing for RLS to scope to. user_profile has
    # a policy, so the INSERT is done here in the same system transaction
    # deliberately — see the comment below.
    async with system_session() as session:
        user = User(
            id=user_id,
            email=body.email.lower(),
            password_hash=hash_password(body.password),
        )
        session.add(user)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="email already registered"
            ) from exc
        # user_profile is tenant-scoped and RLS FORCE is on, so a plain INSERT
        # from a no-context session would be rejected by WITH CHECK. Set the
        # tenant for the remainder of this transaction.
        from stylist_db.session import set_tenant

        await set_tenant(session, user_id)

        # One LiteLLM virtual key per tenant, created at signup with a hard
        # budget (§B3). Enforced by the gateway ON THE CREDENTIAL, so feature
        # code cannot exceed it even with a bug, and spend is attributable.
        #
        # A gateway outage must NOT block signup: registration is the least
        # appropriate moment to fail, and the tag stage already degrades to
        # DEGRADED_TAGGED for a tenant with no key. The key is backfillable.
        litellm_key: str | None = None
        try:
            issued = await gateway.create_virtual_key(
                user_id=user_id, max_budget=settings.free_tier_monthly_budget_usd
            )
            litellm_key = issued.key
        except Exception as exc:
            logger.warning("could not create a virtual key for %s: %s", user_id, exc)

        session.add(
            UserProfile(
                id=uuid.uuid4(),
                user_id=user_id,
                litellm_key=litellm_key,
                litellm_budget_usd=settings.free_tier_monthly_budget_usd,
            )
        )

    pair, _ = issue_token_pair(
        user_id=user_id,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        access_minutes=settings.access_token_minutes,
        refresh_days=settings.refresh_token_days,
    )
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, settings: SettingsDep) -> TokenResponse:
    async with system_session() as session:
        row = await session.execute(select(User).where(User.email == body.email.lower()))
        user = row.scalar_one_or_none()

    # Same error and roughly the same work whether the email is unknown or the
    # password is wrong, so the response does not enumerate accounts.
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    if user.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="account deleted")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account disabled")

    pair, _ = issue_token_pair(
        user_id=user.id,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        access_minutes=settings.access_token_minutes,
        refresh_days=settings.refresh_token_days,
    )
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest,
    settings: SettingsDep,
    redis: QueueRedisDep,
) -> TokenResponse:
    try:
        claims = decode_token(
            body.refresh_token,
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
            expect="refresh",
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=f"invalid refresh token: {exc}"
        ) from exc

    if await redis.is_token_revoked(claims.jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh token revoked"
        )

    # Rotation: the presented refresh token is revoked as it is exchanged, so a
    # stolen copy is single-use and its reuse is detectable.
    remaining = int((claims.expires_at - datetime.now(UTC)).total_seconds())
    if remaining > 0:
        await redis.revoke_token(claims.jti, ttl_seconds=remaining)

    pair, _ = issue_token_pair(
        user_id=claims.user_id,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        access_minutes=settings.access_token_minutes,
        refresh_days=settings.refresh_token_days,
    )
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: RefreshRequest,
    settings: SettingsDep,
    redis: QueueRedisDep,
) -> None:
    """Revoke one refresh token. Access tokens are not revoked — they expire in
    15 minutes and checking them per-request costs a Redis hop on every call."""
    try:
        claims = decode_token(
            body.refresh_token,
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
            expect="refresh",
        )
    except jwt.InvalidTokenError:
        return  # already unusable; nothing to revoke
    remaining = int((claims.expires_at - datetime.now(UTC)).total_seconds())
    if remaining > 0:
        await redis.revoke_token(claims.jti, ttl_seconds=remaining)
