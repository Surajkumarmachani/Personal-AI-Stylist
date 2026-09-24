"""Partner API keys: format, hashing, parsing. Pure — no IO.

FORMAT: `sty_<prefix>_<secret>`
--------------------------------
`prefix` is 12 random url-safe characters, stored in clear and unique, so a
key is found with one indexed lookup rather than by hashing against every
row. `secret` is 32 random bytes. Only sha256(full key) is stored — see 0027
for why sha256 and not bcrypt. The `sty_` tag makes a leaked key greppable
in logs and recognisable to secret scanners.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

TAG = "sty"


@dataclass(frozen=True, slots=True)
class IssuedKey:
    key: str  # shown to the admin ONCE, never stored
    prefix: str
    key_hash: str


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def issue_key() -> IssuedKey:
    # token_urlsafe can emit '_', which is the separator; strip it from the
    # prefix so parsing stays unambiguous. The secret may contain it freely —
    # everything after the second separator is the secret.
    prefix = secrets.token_urlsafe(12).replace("_", "x").replace("-", "y")[:12]
    key = f"{TAG}_{prefix}_{secrets.token_urlsafe(32)}"
    return IssuedKey(key=key, prefix=prefix, key_hash=hash_key(key))


def parse_prefix(key: str) -> str | None:
    """The prefix of a well-formed key, or None."""
    parts = key.strip().split("_", 2)
    if len(parts) != 3 or parts[0] != TAG or len(parts[1]) != 12 or not parts[2]:
        return None
    return parts[1]


def matches(key: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_key(key.strip()), stored_hash)


def partner_email(client_id: str, external_id: str) -> str:
    """A unique, obviously-synthetic address for a partner's user.

    `users.email` is NOT NULL and unique, and a partner's user has no email
    we know. `.invalid` is reserved (RFC 2606), so nothing can ever be sent
    to it, and the hash keeps a partner's own ids out of our email column.
    """
    digest = hashlib.sha256(f"{client_id}:{external_id}".encode()).hexdigest()[:24]
    return f"partner-{digest}@partner.invalid"
