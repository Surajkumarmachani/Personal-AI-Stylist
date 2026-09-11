"""Request outcome counters, for the api_5xx alert.

IN REDIS, NOT IN PROCESS
------------------------
The 5xx rate is a property of the SERVICE. An in-process counter measures only
whichever replica happens to answer /ops/alerts, so a single sick pod among
four is invisible exactly when it matters. Redis is already a dependency and
the write is one pipelined HINCRBY.

BUCKETED BY MINUTE, WITH A TTL
------------------------------
One hash per minute, expiring after two hours. Fixed buckets rather than a
sorted set of individual requests: the rate over the last N minutes is a sum of
N small hashes, the memory is bounded by construction, and nothing needs
trimming. The cost is boundary granularity — a burst spanning a minute edge is
split across two buckets — which does not matter for a rate measured over 15.

FAILING TO COUNT MUST NEVER FAIL A REQUEST
------------------------------------------
Every counter operation is wrapped. Telemetry that can take down the endpoint
it measures is worse than no telemetry: a Redis blip would otherwise turn into
a 500 on every request, and the alert built on it would fire for the reason it
caused.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

KEY_PREFIX = "api:status:"
BUCKET_TTL_SECONDS = 2 * 60 * 60


def _bucket(ts: float) -> str:
    return time.strftime("%Y%m%d%H%M", time.gmtime(ts))


def _klass(status_code: int) -> str:
    return f"{status_code // 100}xx"


class RequestOutcomeMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        try:
            response = await call_next(request)
        except Exception:
            # An unhandled exception IS a 5xx, and it is the most important one
            # to count — Starlette turns it into a 500 response after this
            # middleware has already returned, so counting only `response`
            # would miss precisely the failures the alert exists for.
            await self._record(request, 500)
            raise
        await self._record(request, response.status_code)
        return response

    async def _record(self, request: Request, status_code: int) -> None:
        # /ops/alerts reads these counters; counting its own requests would let
        # a monitoring loop dilute the very rate it is measuring.
        if request.url.path.startswith(("/ops/", "/healthz", "/readyz")):
            return
        cache = getattr(request.app.state, "cache_redis", None)
        if cache is None:
            return
        key = KEY_PREFIX + _bucket(time.time())
        try:
            await cache.incr_bucketed(key, _klass(status_code), ttl_seconds=BUCKET_TTL_SECONDS)
        except Exception as exc:  # pragma: no cover - telemetry must not break requests
            logger.debug("status counter write failed: %s", exc)


async def status_counts(cache: Any, window_minutes: int) -> dict[str, int]:
    """Summed counts per status class over the last `window_minutes`."""
    now = time.time()
    keys = [KEY_PREFIX + _bucket(now - 60 * i) for i in range(window_minutes)]
    totals: dict[str, int] = {}
    try:
        for bucket in await cache.read_buckets(keys):
            for field, value in (bucket or {}).items():
                name = field.decode() if isinstance(field, bytes) else str(field)
                totals[name] = totals.get(name, 0) + int(value)
    except Exception as exc:  # pragma: no cover
        logger.debug("status counter read failed: %s", exc)
    return totals
