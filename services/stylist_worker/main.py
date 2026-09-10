"""arq worker.

PHASE 2 SCOPE: the durable ingest pipeline. Registers three things:

  ingest_photo   the state machine (validate -> sanitise -> moderate ->
                 matte -> persist), resumable and per-stage retried
  relay_outbox   cron, every second: Postgres outbox -> job queue (§C1)
  ping           a database round trip, kept from Phase 1 as a liveness probe

Pool size 10, not the API's 20: a worker burst must not exhaust Postgres
connections and take down the API (§C2).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import text

from stylist_api.settings import get_settings
from stylist_db.session import dispose_engine, init_engine, system_session
from stylist_worker.relay import relay_outbox
from stylist_worker.stages import INGEST_STAGES
from stylist_worker.state_machine import run_pipeline

logger = logging.getLogger(__name__)


async def ingest_photo(
    ctx: dict[str, Any],
    *,
    user_id: str,
    aggregate_id: str,
    payload: dict[str, Any],
) -> dict[str, str]:
    """Drive one photo through the pipeline.

    Idempotent by construction: run_pipeline reloads the job and skips stages
    already completed, so a duplicate delivery from the relay costs one row
    read rather than a repeated matte.
    """
    job_id = uuid.UUID(payload["job_id"])
    final_state = await run_pipeline(uuid.UUID(user_id), job_id, INGEST_STAGES)
    logger.info("ingest_photo job=%s finished at %s", job_id, final_state)
    return {"job_id": str(job_id), "state": final_state}


async def ping(ctx: dict[str, Any]) -> dict[str, str]:
    """Round-trip the database from inside a job.

    Uses system_session (no tenant context) deliberately: it proves the
    connection works without asserting anything about a tenant, and a
    tenant-scoped table would correctly return zero rows here.
    """
    async with system_session() as session:
        row = await session.execute(text("SELECT current_user, version()"))
        db_user, db_version = row.one()
    return {
        "db_user": db_user,
        "db_version": str(db_version).split(",")[0],
        "at": datetime.now(UTC).isoformat(),
    }


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    init_engine(settings.database_url, pool_size=10)  # PROVISIONAL: retune in P9
    logger.info("worker started, environment=%s", settings.environment)


async def shutdown(ctx: dict[str, Any]) -> None:
    await dispose_engine()


class WorkerSettings:
    functions = [ingest_photo, ping]

    # The relay tick. 1s rather than the plan's 250ms: at 250ms this is 4
    # queries/second/replica against Postgres forever, and the ingest UX
    # budget is 10 seconds — a sub-second dispatch delay is invisible inside
    # that, while the query load is not. Revisit in P9 with real numbers.
    # PROVISIONAL: retune in P9.
    cron_jobs = [
        cron(relay_outbox, second=set(range(0, 60)), run_at_startup=True, max_tries=1),
    ]

    on_startup = startup
    on_shutdown = shutdown

    # PROVISIONAL: retune in P9. Derived from the capacity model's claim that
    # a 60-photo burst clears in ~12s at 4 concurrent workers (§B2) — which
    # rests on a 0.8s/photo CPU estimate nothing has measured yet.
    max_jobs = 4

    # arq writes a health record to Redis on this interval, and the container
    # healthcheck reads it. The default is 3600s, which is useless as a
    # liveness signal — a worker could be dead for an hour before anything
    # noticed.
    health_check_interval = 30
    health_check_key = "arq:health-check"

    # An ATTRIBUTE, not a method. arq reads `settings_cls.redis_settings`
    # directly and expects a RedisSettings instance — a @staticmethod here
    # fails at startup with `'staticmethod' object has no attribute 'host'`,
    # and mypy cannot catch it because arq ships no stubs.
    redis_settings = RedisSettings.from_dsn(get_settings().redis_queue_url)
