"""Stage 3: moderate — the gate before anything leaves the VPC (Step 4.4).

The Phase 2 stub is gone; this now runs a real NSFW classifier. Its POSITION is
the requirement: every stage that could export pixels comes after it, so a
flagged upload never reaches a third party. That is why the model is
self-hosted — satisfying this gate with a hosted moderation API would upload
the exact images we are refusing to send anywhere.

A QUARANTINE IS TERMINAL, AUDITED, AND MAKES NO EXTERNAL CALL
------------------------------------------------------------
`Terminal(QUARANTINED)` stops the pipeline before segment, matte, embed or tag.
No VLM call is made, no cost is incurred, and `audit_log` records that a
decision happened — with the score and the model, but no image detail, because
the audit log is retained through erasure and must hold nothing personal.

NON-GARMENT PHOTOS ARE NOT HANDLED HERE
---------------------------------------
A screenshot or a photo of a dog is not a moderation problem, and an NSFW
classifier has no opinion about it. That case is already covered downstream:
segmentation finds no garment class above the area floor and the split routes
to NEEDS_REVIEW with a reason the user can act on. Adding a second, weaker
non-garment check here would duplicate that with worse information.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import text

from stylist_db.session import system_session, tenant_session
from stylist_worker.io_helpers import load_sanitised
from stylist_worker.state_machine import IngestState, JobContext, Stage, Terminal

logger = logging.getLogger(__name__)


async def _run(ctx: JobContext) -> dict[str, Any]:
    store = ctx.scratch.get("store") or _default_store()
    ml = ctx.scratch.get("ml") or _default_ml()

    image = load_sanitised(ctx, store)

    from stylist_clients.ml_client import MLUnavailable
    from stylist_worker.state_machine import Unavailable

    try:
        verdict = await ml.moderate(image_bytes=image)
    except MLUnavailable as exc:
        # FAIL CLOSED by waiting, never by passing.
        #
        # Treating an unavailable moderator as "probably fine" would let
        # unmoderated pixels through to a provider during any ml restart —
        # exactly the window in which a gate is most likely to be bypassed.
        # Waiting costs latency; skipping costs the guarantee.
        raise Unavailable(exc.reason, retry_after=exc.retry_after) from exc

    await _record(ctx, verdict)

    if verdict["verdict"] == "quarantine":
        await _audit(ctx, verdict)
        raise Terminal(
            IngestState.QUARANTINED,
            "this image was flagged by automated moderation and was not processed further",
        )

    if verdict["verdict"] == "review":
        logger.info(
            "job %s flagged for review (nsfw=%.3f) but processing continues",
            ctx.job_id,
            verdict["nsfw_score"],
        )

    return {"moderation": verdict}


async def _record(ctx: JobContext, verdict: dict[str, Any]) -> None:
    """Store the verdict on the garment, and flag review-band items."""
    async with tenant_session(ctx.user_id) as session:
        await session.execute(
            text(
                """
                UPDATE garments
                SET moderation = CAST(:verdict AS jsonb),
                    needs_review = needs_review OR :flag,
                    updated_at = now()
                WHERE original_key = :orig
                """
            ),
            {
                "verdict": json.dumps(verdict),
                "flag": verdict["verdict"] == "review",
                "orig": ctx.payload["key"],
            },
        )


async def _audit(ctx: JobContext, verdict: dict[str, Any]) -> None:
    """Immutable record that a moderation decision was made.

    Runs without tenant context because `audit_log` is deliberately not
    RLS-scoped: it survives the erasure saga (legally required) and therefore
    holds only the pseudonymous user id, a score and a timestamp — no image, no
    key, nothing personal.
    """
    async with system_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO audit_log (id, user_id, action, subject_type, subject_id, detail)
                VALUES (:id, :uid, 'moderation.quarantined', 'job', :jid, CAST(:detail AS jsonb))
                """
            ),
            {
                "id": uuid.uuid4(),
                "uid": ctx.user_id,
                "jid": ctx.job_id,
                "detail": json.dumps(
                    {
                        "nsfw_score": verdict["nsfw_score"],
                        "model": verdict["model"],
                        "thresholds": verdict["thresholds"],
                    }
                ),
            },
        )


def _default_store() -> Any:
    from stylist_worker.deps import get_object_store

    return get_object_store()


def _default_ml() -> Any:
    from stylist_worker.deps import get_ml_client

    return get_ml_client()


moderate_stage = Stage(
    name="moderate",
    completed_state=IngestState.MODERATED,
    run=_run,
    retryable=True,
)
