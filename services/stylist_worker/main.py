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
import os
import uuid
from datetime import UTC, datetime
from typing import Any

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import text

from stylist_api.settings import get_settings
from stylist_db.session import dispose_engine, init_engine, system_session
from stylist_obs import configure_tracing
from stylist_worker.erasure import drain_erasures
from stylist_worker.export import build_export, sweep_expired_exports
from stylist_worker.notify import hourly_digest
from stylist_worker.precompute import nightly_precompute
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
    configure_tracing("stylist-worker")
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    init_engine(settings.database_url, pool_size=10)  # PROVISIONAL: retune in P9
    logger.info("worker started, environment=%s", settings.environment)


async def shutdown(ctx: dict[str, Any]) -> None:
    await dispose_engine()


class WorkerSettings:
    functions = [ingest_photo, build_export, ping]

    # The relay tick. 1s rather than the plan's 250ms: at 250ms this is 4
    # queries/second/replica against Postgres forever, and the ingest UX
    # budget is 10 seconds — a sub-second dispatch delay is invisible inside
    # that, while the query load is not. Revisit in P9 with real numbers.
    # PROVISIONAL: retune in P9.
    cron_jobs = [
        cron(relay_outbox, second=set(range(0, 60)), run_at_startup=True, max_tries=1),
        # 03:15 local. Guarded by pg_try_advisory_lock, so running this on
        # every replica is safe by design rather than by scheduling luck —
        # see precompute.py on why try_ and not the blocking form.
        #
        # max_tries=1: a failed nightly run should wait for tomorrow, not
        # retry into the morning traffic it was scheduled to avoid.
        cron(nightly_precompute, hour={3}, minute={15}, max_tries=1),
        # EVERY HOUR, on purpose. The plan says "07:00 local", and there is no
        # single moment that is 07:00 — it happens 24+ times a day across
        # zones. Each run sends only to tenants for whom it is currently 07:00
        # where they are; a daily cron at a fixed UTC hour would notify
        # everyone at 07:00 in one arbitrary place, i.e. the middle of the
        # night for most of them.
        #
        # No advisory lock, unlike the precompute: `push_send`'s unique index
        # on (user_id, kind, sent_on) already makes a duplicate run a no-op,
        # and it does so PER TENANT, which is stronger than a job-wide lock
        # that would have to be held for the whole sweep.
        cron(hourly_digest, minute={0}, max_tries=1),
        # Erasure has a 30-day LEGAL deadline, so it retries often rather than
        # nightly: a provider outage at 03:15 must not cost a whole day of a
        # deadline nobody can extend. Resumption is free — completed steps are
        # recorded and skipped — so a run with nothing to do is one query.
        cron(drain_erasures, minute={10, 40}, max_tries=1),
        # Hourly, not daily: an export is a complete second copy of a
        # wardrobe, and §C5 gives it 7 days. A daily sweep would leave one
        # sitting up to 24 hours past its stated life — a window that exists
        # for no reason anyone could defend.
        cron(sweep_expired_exports, minute={25}, max_tries=1),
    ]

    on_startup = startup
    on_shutdown = shutdown

    # MATCHED to the ml service's inference capacity, not chosen independently.
    #
    # §C2's bulkheads only work as a pair: N workers each making 4 sequential
    # ml calls against a service that serialises inference means N-1 workers
    # spend their time waiting or being shed. Locally ml runs ONE inference at
    # a time (four models in one process on a small VM), so piling four photos
    # onto it produced read timeouts at whichever stage got unlucky.
    #
    # Production scales ml OUT (View 2: 2-12 pods) and raises this to match.
    # PROVISIONAL: retune in P9 against measured ml throughput.
    max_jobs = int(os.environ.get("WORKER_MAX_JOBS", "2"))

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
