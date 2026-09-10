"""Stage 10: persist — commit the result and emit the follow-on event.

One transaction, two writes: the garment row the user sees, and the outbox event
that tells the rest of the system something changed (§C1). If this crashes
mid-way, neither happened — there is no state in which the wardrobe shows a
cutout but the precompute was never invalidated.

The garment row already exists (created at ingest, state `received`), so this is
an UPDATE. It goes through a tenant-scoped session like every other tenant
write, so RLS applies to the worker exactly as it does to the API. A worker with
a bug that forgets its WHERE clause is contained by the same mechanism.

TWO DIFFERENT `state` COLUMNS, AND THEY MEAN DIFFERENT THINGS
-------------------------------------------------------------
`jobs.state` is pipeline progress: how far this job's stage list has got. It
reaches COMPLETE here because every stage that exists in Phase 2 has run.

`garments.state` is what the wardrobe shows the user, and it is set to `matted`,
NOT `complete` — classification, tagging, embedding and dedupe are real stages
that have not run, and calling the garment complete would be a lie the UI would
then repeat to the user.

Conflating the two is a live bug, not a stylistic point: giving this stage
`completed_state=MATTED` (the same as the matte stage) makes the resume check
`state_rank(MATTED) <= state_rank(MATTED)` true, and persist is silently skipped
on every run — the pipeline would matte an image and never save it.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from stylist_db.outbox import emit
from stylist_db.session import tenant_session
from stylist_worker.state_machine import IngestState, JobContext, Stage

logger = logging.getLogger(__name__)


async def _run(ctx: JobContext) -> dict[str, Any]:
    if ctx.garment_id is None:
        from stylist_worker.state_machine import Terminal

        raise Terminal(IngestState.REJECTED, "job has no garment_id")

    cutout_key: str = ctx.scratch["cutout_key"]

    async with tenant_session(ctx.user_id) as session:
        result = await session.execute(
            text(
                """
                UPDATE garments
                SET cutout_key = :cutout_key,
                    state = :state,
                    updated_at = now()
                WHERE id = :garment_id
                RETURNING id
                """
            ),
            {
                "cutout_key": cutout_key,
                "state": str(IngestState.MATTED),
                "garment_id": ctx.garment_id,
            },
        )
        if result.scalar_one_or_none() is None:
            # RLS returning zero rows here means the garment is not the
            # caller's or was deleted mid-flight. Not retryable.
            from stylist_worker.state_machine import Terminal

            raise Terminal(
                IngestState.REJECTED,
                f"garment {ctx.garment_id} is not visible to its own tenant "
                "(deleted mid-pipeline?)",
            )

        # Same transaction. Phase 6 consumes this to invalidate precomputed
        # outfits; nothing consumes it yet, which is fine — the outbox records
        # intent whether or not a consumer exists.
        await emit(
            session,
            aggregate_id=ctx.garment_id,
            user_id=ctx.user_id,
            event_type="garment.cutout_ready",
            payload={
                "garment_id": str(ctx.garment_id),
                "job_id": str(ctx.job_id),
                "cutout_key": cutout_key,
            },
        )

    logger.info("persisted job=%s garment=%s", ctx.job_id, ctx.garment_id)
    return {"persisted": True}


persist_stage = Stage(
    name="persist",
    # COMPLETE, not MATTED — see the module docstring. The job has finished
    # every stage Phase 2 defines; the garment row it wrote says `matted`.
    completed_state=IngestState.COMPLETE,
    run=_run,
    retryable=True,
)
