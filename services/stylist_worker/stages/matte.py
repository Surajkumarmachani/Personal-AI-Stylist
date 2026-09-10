"""Stage 5: matte — background removal, producing an RGBA cutout.

Note there is no stage 4 (segment) in Phase 2. Segmentation splits a
multi-garment photo into N masks and lands in Phase 3; until then a flat-lay is
matted whole, which is the correct treatment for one garment on a plain
background and is exactly the Phase 2 exit criterion.

WHY THIS CALLS THE ML SERVICE INSTEAD OF IMPORTING REMBG
--------------------------------------------------------
Matting is CPU-bound model inference; the worker is I/O-bound orchestration.
They scale on different axes, and coupling them means buying CPU (later, GPU)
to sit waiting on S3 — the split View 2 exists to prevent. `services/stylist_ml`
already owns model execution and holds no database credentials, so the pixels
are processed by a service that structurally cannot read the wardrobe.

This is where the plan's Step 2.3 and Step 3.1 meet: the plan lists matting in
Phase 2 and the ml service in Phase 3. Building the endpoint now rather than
writing rembg into the worker and moving it later avoids doing the work twice,
and matches the deployment topology either way.
"""

from __future__ import annotations

import logging
from typing import Any

from stylist_worker.state_machine import IngestState, JobContext, Stage

logger = logging.getLogger(__name__)

# A cutout whose alpha covers almost nothing means the matte failed rather than
# succeeded at finding no garment — usually a dark-on-dark frame. Route to
# review instead of storing a blank PNG the user cannot interpret.
MIN_ALPHA_COVERAGE = 0.02


async def _run(ctx: JobContext) -> dict[str, Any]:
    store = ctx.scratch.get("store") or _default_store()
    ml = ctx.scratch.get("ml") or _default_ml()

    sanitised_key: str = ctx.scratch["sanitised_key"]

    result = await ml.matte(image_bytes=ctx.scratch["sanitised_bytes"])

    if result.alpha_coverage < MIN_ALPHA_COVERAGE:
        from stylist_worker.state_machine import Terminal

        raise Terminal(
            IngestState.NEEDS_REVIEW,
            f"matte found almost no subject (alpha coverage "
            f"{result.alpha_coverage:.3%}); needs a manual crop",
        )

    cutout_key = f"cutouts/{ctx.user_id}/{ctx.job_id}.png"
    store.put_bytes(cutout_key, result.cutout_png, content_type="image/png")

    logger.info(
        "matted job=%s coverage=%.1f%% trimmed=%dx%d",
        ctx.job_id,
        result.alpha_coverage * 100,
        result.width,
        result.height,
    )
    return {
        "cutout_key": cutout_key,
        "alpha_coverage": result.alpha_coverage,
        "cutout_width": result.width,
        "cutout_height": result.height,
        "matte_model": result.model,
        "source_key": sanitised_key,
    }


def _default_store() -> Any:
    from stylist_worker.deps import get_object_store

    return get_object_store()


def _default_ml() -> Any:
    from stylist_worker.deps import get_ml_client

    return get_ml_client()


matte_stage = Stage(
    name="matte",
    completed_state=IngestState.MATTED,
    run=_run,
    retryable=True,  # model service restarts and network blips are transient
)
