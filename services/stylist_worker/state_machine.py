"""Durable ingest state machine (View 5).

An ingest job is a state machine whose states persist to `jobs`, not a function
call. Two properties follow from that and neither is optional:

RESUME, NOT RESTART
    `jobs.state` is the last SUCCESSFULLY COMPLETED state. A worker that dies
    mid-pipeline is replaced by one that reloads the row and skips every stage
    already done. Restarting from the top would re-run segmentation and
    matting — the expensive stages — for a failure in a late cheap one.

RETRY PER STAGE, NOT PER JOB
    A VLM timeout must never re-run segmentation. Attempts are counted per
    stage in `jobs.stage_attempts`, so a transient blip in one provider costs
    one retry of one stage rather than a multiple of the whole pipeline's CPU.

Backoff is exponential with FULL JITTER (random between 0 and the ceiling), not
plain exponential. Without jitter, N jobs failing on the same provider outage
retry in lockstep and hammer it in synchronised waves the moment it recovers.

EVERY WRITE IN HERE IS TENANT-SCOPED, AND THAT IS NOT OPTIONAL
--------------------------------------------------------------
`jobs` is an RLS-protected table. The worker connects as `stylist_app`
(NOSUPERUSER, NOBYPASSRLS) exactly like the API does, so a statement issued
without tenant context matches zero rows — no error, no warning, just silence.
An early version of this file used `system_session()` for the job updates and
every transition silently did nothing; the pipeline appeared to run and no
state ever moved.

So `user_id` is threaded through from the arq job arguments (the relay reads it
from the outbox row, which is deliberately not RLS-scoped) and every statement
runs inside `tenant_session`. The upside of paying that cost: a worker bug that
forgets a WHERE clause is contained by the same mechanism that protects the
API, rather than being unconstrained because it is "backend" code.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import Text, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from stylist_db.session import tenant_session

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 2.0


class IngestState(StrEnum):
    """Every state in View 5, including ones no Phase 2 stage produces yet.

    The full set is declared now on purpose: `jobs.state` is a plain varchar
    (not a Postgres enum) precisely so Phase 3 can start emitting SEGMENTED
    without a migration, and having the constant here means the ordering table
    below is the single place that has to change.
    """

    RECEIVED = "received"
    VALIDATED = "validated"
    SANITISED = "sanitised"
    MODERATED = "moderated"
    SEGMENTED = "segmented"  # Phase 3
    MATTED = "matted"
    CLASSIFIED = "classified"  # Phase 3
    TAGGED = "tagged"  # Phase 4
    EMBEDDED = "embedded"  # Phase 3
    DEDUPED = "deduped"  # Phase 5
    COMPLETE = "complete"

    # Terminal / off-ramp states.
    REJECTED = "rejected"
    QUARANTINED = "quarantined"
    NEEDS_REVIEW = "needs_review"
    DEGRADED_TAGGED = "degraded_tagged"
    DUPLICATE_SUSPECT = "duplicate_suspect"


# Progress order. A stage is skipped on resume when its completed_state is at
# or below the job's current state.
#
# WARNING for Phase 3: inserting SEGMENTED between MODERATED and MATTED changes
# the meaning of this list for jobs already parked at MATTED — they would skip
# segmentation entirely. When that stage lands, either drain the queue first or
# backfill in-flight jobs. This is the one place where the state machine is not
# self-migrating.
STATE_ORDER: tuple[IngestState, ...] = (
    IngestState.RECEIVED,
    IngestState.VALIDATED,
    IngestState.SANITISED,
    IngestState.MODERATED,
    IngestState.SEGMENTED,
    IngestState.MATTED,
    IngestState.CLASSIFIED,
    IngestState.TAGGED,
    IngestState.EMBEDDED,
    IngestState.DEDUPED,
    IngestState.COMPLETE,
)

TERMINAL_STATES: frozenset[IngestState] = frozenset(
    {
        IngestState.COMPLETE,
        IngestState.REJECTED,
        IngestState.QUARANTINED,
        IngestState.NEEDS_REVIEW,
        IngestState.DUPLICATE_SUSPECT,
    }
)


def state_rank(state: str) -> int:
    """Position in STATE_ORDER, or -1 for off-ramp states."""
    try:
        return STATE_ORDER.index(IngestState(state))
    except ValueError:
        return -1


class Terminal(Exception):  # noqa: N818 - name matches the plan's stage contract
    """Stop the pipeline and park the job in a user-visible terminal state.

    Not a failure to retry: a non-garment photo or a corrupt file will fail
    identically on every attempt, and retrying it three times just wastes CPU
    and delays telling the user.
    """

    def __init__(self, state: IngestState, reason: str) -> None:
        super().__init__(reason)
        self.state = state
        self.reason = reason


class Exhausted(Exception):  # noqa: N818 - name matches the plan's stage contract
    """All attempts for one stage failed. The job goes to the DLQ with its last
    good state, so it is replayable from there rather than from the start."""

    def __init__(self, stage: str, attempts: int, last_error: str) -> None:
        super().__init__(f"{stage} failed after {attempts} attempts: {last_error}")
        self.stage = stage
        self.attempts = attempts
        self.last_error = last_error


@dataclass(frozen=True, slots=True)
class JobContext:
    """What a stage is given. Deliberately small and serialisable."""

    job_id: uuid.UUID
    user_id: uuid.UUID
    garment_id: uuid.UUID | None
    payload: dict[str, Any]
    state: str
    # Accumulated stage outputs, e.g. the sanitised object key produced by one
    # stage and consumed by the next.
    scratch: dict[str, Any]


StageFn = Callable[[JobContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class Stage:
    name: str
    completed_state: IngestState
    run: StageFn
    # Stages that talk to a third party or burn real CPU are worth retrying;
    # pure local validation is not (it will fail the same way every time).
    retryable: bool = True


async def _sleep_backoff(attempt: int) -> None:
    """Exponential ceiling with full jitter: sleep ~U(0, base * 2^(n-1))."""
    ceiling = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
    await asyncio.sleep(random.uniform(0, ceiling))


async def _run_stage_with_retry(
    stage: Stage, ctx: JobContext, attempts_so_far: int
) -> dict[str, Any]:
    attempt = attempts_so_far
    last_error = ""
    max_attempts = MAX_ATTEMPTS if stage.retryable else 1

    while attempt < max_attempts:
        attempt += 1
        try:
            return await stage.run(ctx)
        except Terminal:
            raise  # never retried; the outcome is deterministic
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "stage %s attempt %d/%d failed: %s",
                stage.name,
                attempt,
                max_attempts,
                last_error,
            )
            await _record_attempt(ctx.user_id, ctx.job_id, stage.name, attempt, last_error)
            if attempt >= max_attempts:
                break
            await _sleep_backoff(attempt)

    raise Exhausted(stage.name, attempt, last_error)


async def _record_attempt(
    user_id: uuid.UUID, job_id: uuid.UUID, stage: str, attempt: int, error: str
) -> None:
    """Persist the attempt count so a restarted worker does not reset it.

    Without this, a worker that dies during retries hands the next worker a
    fresh budget of 3, and a permanently-failing stage never reaches the DLQ.
    """
    # Bind params are given explicit Postgres types rather than written as
    # inline `::int` / `ARRAY[...]` casts. Under asyncpg, a `::` cast inside a
    # text() statement collides with SQLAlchemy's own `:param` parsing and the
    # stray colon reaches the server as a syntax error.
    stmt = text(
        """
        UPDATE jobs
        SET stage_attempts = jsonb_set(stage_attempts, :path, :value, true),
            last_error = :error,
            updated_at = now()
        WHERE id = :job_id
        """
    ).bindparams(
        bindparam("path", type_=ARRAY(Text)),
        bindparam("value", type_=JSONB),
    )
    async with tenant_session(user_id) as session:
        await session.execute(
            stmt,
            {
                "path": [stage],
                "value": attempt,
                "error": error[:2000],
                "job_id": job_id,
            },
        )


async def _transition(
    user_id: uuid.UUID, job_id: uuid.UUID, state: IngestState, *, reason: str | None = None
) -> None:
    async with tenant_session(user_id) as session:
        await session.execute(
            text(
                """
                UPDATE jobs
                SET state = :state,
                    last_error = COALESCE(:reason, last_error),
                    updated_at = now()
                WHERE id = :job_id
                """
            ),
            {"state": str(state), "reason": reason, "job_id": job_id},
        )


async def _to_dlq(user_id: uuid.UUID, job_id: uuid.UUID, last_good_state: str, error: str) -> None:
    """Park the job. `state` is left at the last good value on purpose — the
    job is replayable from there, and the DLQ marker is a separate column."""
    async with tenant_session(user_id) as session:
        await session.execute(
            text(
                """
                UPDATE jobs
                SET dlq_at = now(), last_error = :error, updated_at = now()
                WHERE id = :job_id
                """
            ),
            {"error": error[:2000], "job_id": job_id},
        )
    from stylist_worker.alerts import alert

    alert(
        "dlq.job_parked",
        job_id=str(job_id),
        last_good_state=last_good_state,
        error=error[:500],
    )


async def load_job(user_id: uuid.UUID, job_id: uuid.UUID) -> JobContext | None:
    """Read the job within its tenant.

    user_id comes from the arq job arguments rather than being looked up: with
    RLS on `jobs`, there is no way to read the row in order to discover which
    tenant it belongs to. The outbox row carried it, so the relay passes it on.
    """
    async with tenant_session(user_id) as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT id, user_id, garment_id, payload, state, dlq_at "
                        "FROM jobs WHERE id = :job_id"
                    ),
                    {"job_id": job_id},
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        return None
    if row["dlq_at"] is not None:
        logger.info("job %s is in the DLQ; not running", job_id)
        return None
    return JobContext(
        job_id=row["id"],
        user_id=row["user_id"],
        garment_id=row["garment_id"],
        payload=dict(row["payload"]),
        state=row["state"],
        scratch={},
    )


async def _attempts_for(user_id: uuid.UUID, job_id: uuid.UUID, stage: str) -> int:
    async with tenant_session(user_id) as session:
        row = await session.execute(
            text("SELECT COALESCE(stage_attempts->>:stage, '0') FROM jobs WHERE id = :job_id"),
            {"stage": stage, "job_id": job_id},
        )
        return int(row.scalar_one() or 0)


async def run_pipeline(user_id: uuid.UUID, job_id: uuid.UUID, stages: tuple[Stage, ...]) -> str:
    """Drive one job to a terminal state. Returns the final state.

    Safe to call again at any point: completed stages are skipped, so a
    duplicate delivery from the relay is a cheap no-op rather than repeated
    work.
    """
    ctx = await load_job(user_id, job_id)
    if ctx is None:
        return "skipped"

    if IngestState(ctx.state) in TERMINAL_STATES:
        logger.info("job %s already terminal at %s", job_id, ctx.state)
        return ctx.state

    scratch: dict[str, Any] = {}
    current_state = ctx.state

    for stage in stages:
        if state_rank(stage.completed_state) <= state_rank(current_state):
            logger.debug("job %s skipping %s (already at %s)", job_id, stage.name, current_state)
            continue

        stage_ctx = JobContext(
            job_id=ctx.job_id,
            user_id=ctx.user_id,
            garment_id=ctx.garment_id,
            payload=ctx.payload,
            state=current_state,
            scratch=scratch,
        )
        attempts_so_far = await _attempts_for(user_id, job_id, stage.name)

        try:
            result = await _run_stage_with_retry(stage, stage_ctx, attempts_so_far)
        except Terminal as term:
            await _transition(user_id, job_id, term.state, reason=term.reason)
            logger.info("job %s terminal: %s (%s)", job_id, term.state, term.reason)
            return str(term.state)
        except Exhausted as exhausted:
            await _to_dlq(user_id, job_id, current_state, str(exhausted))
            return "dlq"

        scratch.update(result)
        await _transition(user_id, job_id, stage.completed_state)
        current_state = str(stage.completed_state)

    return current_state


async def mark_garment_state(user_id: uuid.UUID, garment_id: uuid.UUID, state: str) -> None:
    """Mirror pipeline progress onto the garment the user actually sees.

    Tenant-scoped on purpose: this is the row the wardrobe grid reads, so it
    goes through RLS like every other tenant write.
    """
    async with tenant_session(user_id) as session:
        await session.execute(
            text("UPDATE garments SET state = :state, updated_at = now() WHERE id = :gid"),
            {"state": state, "gid": garment_id},
        )
