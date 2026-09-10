"""Liveness and readiness.

/healthz is liveness: the process is up. It must NOT check dependencies, or a
Postgres blip restarts every pod and turns a brief degradation into an outage.
/readyz is readiness: dependencies reachable, so traffic can be routed here.

READINESS MUST TRAVERSE THE NETWORK THE CALLER USES.
ml is checked here over compose/cluster DNS, exactly as the worker reaches it.
It is not checked because the api itself calls ml — it does not — but because
every other probe in the system is blind to ml being unreachable:

  - ml's own container healthcheck hits localhost:8000 from INSIDE the
    container, so it passes while the container is detached from the network;
  - the worker has no probe at all, and discovers the problem only by failing
    real ingests.

That combination actually happened: `readyz` on the published port returned 200
for ~30 minutes while every worker call failed with ConnectError, because the
container was answering the host but had no network alias. A probe that does
not use the caller's path cannot see that class of failure.

ml is reported but NOT fatal to readiness: the ingest pipeline degrades
deliberately when ml is down (Unavailable backpressure, then DEGRADED_TAGGED),
and the API still serves reads, corrections and boards. Failing readiness would
pull the whole API out of rotation over a degradation it is designed to absorb.
"""

from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, Response, status
from sqlalchemy import text

from stylist_api.deps import CacheRedisDep, QueueRedisDep
from stylist_db.session import system_session

ML_BASE_URL = os.environ.get("ML_BASE_URL", "http://ml:8000")
# Short: this is a probe, not a request path. A slow ml must not make readiness
# itself time out and take the API out of rotation.
ML_PROBE_TIMEOUT = 2.0

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

    # Over DNS, on the worker's path. Reported separately from `checks` so it
    # never gates readiness (see the module docstring).
    ml_status: str
    try:
        async with httpx.AsyncClient(timeout=ML_PROBE_TIMEOUT) as ml_client:
            ml_resp = await ml_client.get(f"{ML_BASE_URL}/readyz")
        ml_body = ml_resp.json()
        if ml_resp.status_code == 200 and ml_body.get("ready"):
            ml_status = "ok"
        else:
            # Reachable but not serving: models still building, or degraded.
            ml_status = f"not ready: {ml_body}"
    except Exception as exc:
        # The failure that was previously invisible to every probe.
        ml_status = f"unreachable: {type(exc).__name__}"
    dependencies: dict[str, str] = {"ml": ml_status}

    ready = all(v == "ok" for v in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"ready": ready, "checks": checks, "dependencies": dependencies}
