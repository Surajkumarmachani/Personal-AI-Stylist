"""Redis adapters: idempotency keys and the JWT revocation list.

TWO LOGICAL DATABASES, NEVER ONE
--------------------------------
db0 is the queue and MUST run `noeviction`. db1 is the cache and runs
`allkeys-lru`. Sharing one database means an LRU eviction can silently delete
queued jobs — a failure that has taken down production systems and which is
invisible until work quietly stops happening (§C2).

Locally we go further and run two separate containers, so the mistake is
impossible to make rather than merely configured against.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from redis.asyncio import Redis

IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60  # 24h, per Step 1.5


@dataclass(frozen=True, slots=True)
class IdempotencyResult:
    """The job ids for one Idempotency-Key.

    A LIST, not a single id, because one ingest request carries up to 60 photos
    and a replay has to return every job it created — not just the first. An
    earlier version stored one id and suffixed the rest (`key:1`, `key:2`, ...),
    which made a replay of a 10-photo batch return 1 job id and silently drop
    the other 9 from the client's view.
    """

    job_ids: tuple[uuid.UUID, ...]
    created: bool  # False => key already seen; job_ids are the ORIGINALS


class QueueRedis:
    """db0. Job queue + idempotency. noeviction — nothing here is disposable."""

    def __init__(self, url: str) -> None:
        self._r: Redis = Redis.from_url(url, decode_responses=True)

    async def close(self) -> None:
        await self._r.aclose()

    async def ping(self) -> bool:
        return bool(await self._r.ping())

    @staticmethod
    def _idem_key(user_id: uuid.UUID | str, key: str) -> str:
        # Namespaced by tenant: two users may legitimately send the same
        # Idempotency-Key, and they must not collide.
        return f"idem:{user_id}:{key}"

    async def claim_idempotency_key(
        self, *, user_id: uuid.UUID | str, key: str, job_ids: Sequence[uuid.UUID]
    ) -> IdempotencyResult:
        """SETNX the key to the batch's job ids.

        First caller wins and gets created=True. Any repeat within the TTL gets
        created=False and the ORIGINAL ids, in the original order — so a
        retried request returns the same jobs rather than starting a second
        pipeline over the same photos.

        Order is preserved because the client pairs the returned ids with the
        uploads it sent, and a reordered list would attach progress streams to
        the wrong photos.
        """
        redis_key = self._idem_key(user_id, key)
        payload = json.dumps([str(j) for j in job_ids])
        won = await self._r.set(redis_key, payload, nx=True, ex=IDEMPOTENCY_TTL_SECONDS)
        if won:
            return IdempotencyResult(job_ids=tuple(job_ids), created=True)

        existing = await self._r.get(redis_key)
        if existing is None:
            # Expired between SET and GET. Treat as a fresh claim; the DB
            # unique constraint on (user_id, idempotency_key) is the durable
            # backstop that still stops a genuine double-insert.
            return IdempotencyResult(job_ids=tuple(job_ids), created=True)
        return IdempotencyResult(
            job_ids=tuple(uuid.UUID(j) for j in json.loads(existing)), created=False
        )

    async def revoke_token(self, jti: str, *, ttl_seconds: int) -> None:
        """Deny-list a refresh token. TTL matches the token's remaining life, so
        the list cannot grow without bound."""
        await self._r.set(f"jwt:revoked:{jti}", "1", ex=ttl_seconds)

    async def is_token_revoked(self, jti: str) -> bool:
        return bool(await self._r.exists(f"jwt:revoked:{jti}"))


class CacheRedis:
    """db1. Rationales, boards, weather/context. allkeys-lru — all disposable."""

    def __init__(self, url: str) -> None:
        self._r: Redis = Redis.from_url(url, decode_responses=True)

    async def close(self) -> None:
        await self._r.aclose()

    async def ping(self) -> bool:
        return bool(await self._r.ping())

    async def get(self, key: str) -> str | None:
        return cast(str | None, await self._r.get(key))

    async def set(self, key: str, value: str, *, ttl_seconds: int) -> None:
        await self._r.set(key, value, ex=ttl_seconds)

    async def incr_window(self, key: str, *, ttl_seconds: int) -> int:
        """Count one event in a fixed window; the window expires on its own.

        For per-client rate limits. A cache flush resets every window, which
        errs towards letting requests through — the right way to fail for a
        limit whose job is fairness, not security.
        """
        pipe = self._r.pipeline()
        pipe.incr(key)
        pipe.expire(key, ttl_seconds, nx=True)
        count, _ = await pipe.execute()
        return int(count)

    # ---- request-outcome counters (api_5xx alert) -------------------------
    #
    # On the wrapper rather than exposing the raw client: the point of this
    # class is that callers cannot reach Redis primitives directly, and the
    # middleware reaching through it for .pipeline() silently did nothing —
    # the attribute does not exist, and the middleware swallows its own errors
    # by design so that telemetry can never fail a request. The counters read
    # as empty, and the alert built on them could not fire.

    async def incr_bucketed(
        self, key: str, field: str, *, ttl_seconds: int, amount: int = 1
    ) -> None:
        """Increment one field of a bucketed hash, refreshing its expiry.

        `amount` exists because a counter that can only step by 1 forces the
        caller to record EVENTS when the SLI is about ITEMS. The Phase 7
        rationale cache hit its exact trap: one request looking up 5 outfits
        and finding 1 incremented `hit` once and `miss` once, so a true 20% hit
        rate read as 50% — the one number the alert exists to detect.
        """
        pipe = self._r.pipeline()
        pipe.hincrby(key, field, amount)
        pipe.expire(key, ttl_seconds)
        await pipe.execute()

    async def read_buckets(self, keys: list[str]) -> list[dict[str, str]]:
        """Read many bucket hashes in one round trip."""
        pipe = self._r.pipeline()
        for key in keys:
            pipe.hgetall(key)
        return cast(list[dict[str, str]], await pipe.execute())
