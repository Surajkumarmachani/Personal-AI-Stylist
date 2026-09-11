"""Stage 10: persist — finalise and announce.

Every earlier stage already wrote its own results, so this is deliberately
thin. That ordering is the point: partial usefulness (View 5) means the user
watches items appear and fill in, not a blank grid until the whole chain
finishes. Deferring all writes to the end would also mean a crash at stage 8
threw away seven stages of work.

What is left for the end is the part that must be atomic with the announcement:
the terminal state transition and the outbox event, in one transaction (§C1).
There is no state in which the wardrobe shows a finished garment while nothing
downstream was ever told it changed.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import text

from stylist_db.outbox import emit
from stylist_db.session import tenant_session
from stylist_worker.state_machine import IngestState, JobContext, Stage, Terminal

logger = logging.getLogger(__name__)


async def _run(ctx: JobContext) -> dict[str, Any]:
    if ctx.garment_id is None:
        raise Terminal(IngestState.REJECTED, "job has no garment_id")

    async with tenant_session(ctx.user_id) as session:
        rows = (
            (
                await session.execute(
                    text(
                        """
                    SELECT id, slot, primary_colour, cutout_key,
                           embedding IS NOT NULL AS has_embedding
                    FROM garments
                    WHERE original_key = :orig AND is_active
                    """
                    ),
                    {"orig": ctx.payload["key"]},
                )
            )
            .mappings()
            .all()
        )

        if not rows:
            # RLS returning nothing here means the garments are not the
            # caller's or were deleted mid-flight. Not retryable.
            raise Terminal(
                IngestState.REJECTED,
                "no garments visible for this upload (deleted mid-pipeline?)",
            )

        for row in rows:
            # `matted` for the garment, not `complete`: `pattern` is still
            # deliberately unset pending the golden set, so calling the garment
            # complete would be a lie the UI repeats to the user.
            #
            # The WHERE clause preserves decisions earlier stages already made
            # about this GARMENT. dedupe parks a near-duplicate at
            # DUPLICATE_SUSPECT, and without the guard this statement — which
            # runs after it — silently reset that to `matted`: the duplicate
            # was detected, recorded in `duplicate_of`, and then presented to
            # the user as an ordinary garment with no question attached.
            # A job completing and a garment being unremarkable are different
            # facts, and only the first one is persist's to assert.
            await session.execute(
                text(
                    """
                    UPDATE garments
                    SET state = :state, updated_at = now()
                    WHERE id = :gid
                      AND state NOT IN ('duplicate_suspect', 'rejected', 'quarantined')
                    """
                ),
                {"state": str(IngestState.MATTED), "gid": row["id"]},
            )

            # One event per garment, so a multi-garment photo invalidates
            # precompute for each of them rather than once for the photo.
            await emit(
                session,
                aggregate_id=uuid.UUID(str(row["id"])),
                user_id=ctx.user_id,
                event_type="garment.cutout_ready",
                payload={
                    "garment_id": str(row["id"]),
                    "job_id": str(ctx.job_id),
                    "slot": row["slot"],
                    "primary_colour": row["primary_colour"],
                    "cutout_key": row["cutout_key"],
                    "has_embedding": bool(row["has_embedding"]),
                },
            )

    logger.info("job %s persisted %d garment(s)", ctx.job_id, len(rows))
    return {"persisted": len(rows)}


persist_stage = Stage(
    name="persist",
    # COMPLETE for the JOB: every stage Phase 3 defines has run. The GARMENTS
    # are at `matted`, awaiting Phase 4's VLM tags.
    completed_state=IngestState.COMPLETE,
    run=_run,
    retryable=True,
)
