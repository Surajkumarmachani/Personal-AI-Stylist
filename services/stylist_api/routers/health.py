"""Liveness and readiness.

/healthz is liveness: the process is up. It must NOT check dependencies, or a
Postgres blip restarts every pod and turns a brief degradation into an outage.
/readyz is readiness: dependencies reachable, so traffic can be routed here.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from stylist_api.deps import CacheRedisDep, QueueRedisDep
from stylist_db.session import system_session

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(
    response: Response,
    queue: QueueRedisDep,
    cache: CacheRedisDep,
) -> dict[str, object]:
    checks: dict[str, object] = {}

    try:
        async with system_session() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {type(exc).__name__}"

    for name, client in (("redis_queue", queue), ("redis_cache", cache)):
        try:
            await client.ping()
            checks[name] = "ok"
        except Exception as exc:
            checks[name] = f"error: {type(exc).__name__}"

    ready = all(v == "ok" for v in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"ready": ready, "checks": checks}
