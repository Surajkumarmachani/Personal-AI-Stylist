"""Password hashing and JWT issue/verify.

Access tokens are 15 minutes and are NOT checked against the revocation list —
that would be a Redis round trip on every request for a token that expires
inside the window anyway. Refresh tokens are 30 days, carry a `jti`, and ARE
checked, because a 30-day credential must be revocable (logout, password
change, erasure step 1).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import bcrypt
import jwt

TokenType = Literal["access", "refresh"]


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int  # seconds until the ACCESS token expires


@dataclass(frozen=True, slots=True)
class TokenClaims:
    user_id: uuid.UUID
    token_type: TokenType
    jti: str
    expires_at: datetime


def hash_password(password: str) -> str:
    # bcrypt truncates silently at 72 bytes, so reject long inputs rather than
    # letting two different passwords hash identically.
    encoded = password.encode("utf-8")
    if len(encoded) > 72:
        raise ValueError("password must be at most 72 bytes")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def _encode(
    *,
    user_id: uuid.UUID,
    token_type: TokenType,
    lifetime: timedelta,
    secret: str,
    algorithm: str,
) -> tuple[str, str, datetime]:
    now = datetime.now(UTC)
    expires_at = now + lifetime
    jti = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "typ": token_type,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=algorithm), jti, expires_at


def issue_token_pair(
    *,
    user_id: uuid.UUID,
    secret: str,
    algorithm: str = "HS256",
    access_minutes: int = 15,
    refresh_days: int = 30,
) -> tuple[TokenPair, str]:
    """Returns the pair plus the refresh token's jti, so the caller can revoke
    it later without decoding the token again."""
    access, _, access_exp = _encode(
        user_id=user_id,
        token_type="access",
        lifetime=timedelta(minutes=access_minutes),
        secret=secret,
        algorithm=algorithm,
    )
    refresh, refresh_jti, _ = _encode(
        user_id=user_id,
        token_type="refresh",
        lifetime=timedelta(days=refresh_days),
        secret=secret,
        algorithm=algorithm,
    )
    expires_in = int((access_exp - datetime.now(UTC)).total_seconds())
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=expires_in), refresh_jti


def decode_token(
    token: str, *, secret: str, algorithm: str = "HS256", expect: TokenType | None = None
) -> TokenClaims:
    """Raises jwt.InvalidTokenError (or a subclass) on anything wrong.

    `expect` matters: an access token must never be accepted where a refresh
    token is required, or the 15-minute lifetime becomes decorative.
    """
    payload = jwt.decode(token, secret, algorithms=[algorithm])
    # Validate `typ` against the known set rather than trusting whatever the
    # payload carries — an unrecognised value must be an error, not something
    # that flows into TokenClaims and gets compared later.
    if payload.get("typ") not in ("access", "refresh"):
        raise jwt.InvalidTokenError(f"unknown token type: {payload.get('typ')!r}")
    token_type: TokenType = payload["typ"]
    if expect is not None and token_type != expect:
        raise jwt.InvalidTokenError(f"expected a {expect} token, got {token_type}")
    return TokenClaims(
        user_id=uuid.UUID(payload["sub"]),
        token_type=token_type,
        jti=payload["jti"],
        expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
    )
