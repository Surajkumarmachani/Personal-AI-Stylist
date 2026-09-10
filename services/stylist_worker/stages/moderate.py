"""Stage 3: moderate.

PHASE 2: a pass-through stub. Phase 4.4 replaces the body with a real local
NSFW + non-garment classifier.

THE STUB IS WIRED IN NOW ON PURPOSE, AND THAT IS NOT THE SAME AS BUILDING AHEAD
------------------------------------------------------------------------------
This stage's position in the pipeline is a policy requirement: it sits before
every stage that could send pixels off our infrastructure, so a rejected upload
never leaves the VPC (View 1, View 5). Retrofitting a gate *before* existing
third-party calls means auditing every call site and getting the ordering right
under pressure; leaving a no-op in the correct position costs nothing and makes
Phase 4.4 a change of one function body.

The distinction from a fake pipeline stage: this does not pretend to moderate.
It returns an explicit `moderation: "skipped_phase2"` verdict that is recorded
on the job, so nothing downstream can mistake an unmoderated image for a
cleared one.
"""

from __future__ import annotations

import logging
from typing import Any

from stylist_worker.state_machine import IngestState, JobContext, Stage

logger = logging.getLogger(__name__)


async def _run(ctx: JobContext) -> dict[str, Any]:
    # Phase 4.4: classify here; on a flag, raise
    #   Terminal(IngestState.QUARANTINED, reason)
    # and write an audit_log entry. No third-party call is ever made for a
    # quarantined image, which is both the policy requirement and a cost
    # control.
    logger.debug("moderation stub passing job=%s", ctx.job_id)
    return {"moderation": "skipped_phase2"}


moderate_stage = Stage(
    name="moderate",
    completed_state=IngestState.MODERATED,
    run=_run,
    retryable=False,  # local and deterministic once implemented
)
