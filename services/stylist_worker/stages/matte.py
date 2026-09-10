"""Stage 5: matte — one RGBA cutout per garment.

Now mask-driven. Segmentation has already decided what the garments are, so
each one is matted against its own mask: on a mirror selfie, asking u2net to
matte the whole frame keeps the entire outfit, so "the shirt" would come back
as shirt-plus-trousers.

WHY THIS RUNS IN THE ML SERVICE INSTEAD OF IMPORTING REMBG
----------------------------------------------------------
Matting is CPU-bound model inference; the worker is I/O-bound orchestration.
They scale on different axes, and coupling them means buying CPU (later, GPU)
to sit waiting on S3 — the split View 2 exists to prevent. `stylist_ml` also
holds no database or storage credentials, so the process handling user pixels
structurally cannot read the wardrobe.

ONE BAD MASK MUST NOT FAIL THE WHOLE PHOTO
------------------------------------------
A four-garment selfie where one mask mattes to almost nothing should yield
three good garments and one flagged for review — not four failures. So a
low-coverage cutout marks that garment `needs_review` and keeps going; only
losing EVERY garment is terminal.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from stylist_db.session import tenant_session
from stylist_worker.io_helpers import load_sanitised
from stylist_worker.keys import cutout_key as make_cutout_key
from stylist_worker.state_machine import IngestState, JobContext, Stage, Terminal

logger = logging.getLogger(__name__)

# Below this share of alpha the matte found no subject rather than a small one
# — usually a dark-garment-on-dark-background frame. Storing a near-blank PNG
# would give the user an empty tile with no explanation.
MIN_ALPHA_COVERAGE = 0.02


async def _run(ctx: JobContext) -> dict[str, Any]:
    store = ctx.scratch.get("store") or _default_store()
    ml = ctx.scratch.get("ml") or _default_ml()

    records = await _records(ctx)
    image = load_sanitised(ctx, store)

    from stylist_clients.ml_client import MLUnavailable
    from stylist_worker.state_machine import Unavailable

    matted: list[dict[str, Any]] = []
    for record in records:
        mask = store.get_bytes(record["mask_key"]) if record.get("mask_key") else None
        try:
            result = await ml.matte(image_bytes=image, mask_png=mask)
        except MLUnavailable as exc:
            raise Unavailable(exc.reason, retry_after=exc.retry_after) from exc

        needs_review = result.alpha_coverage < MIN_ALPHA_COVERAGE
        key = make_cutout_key(ctx.user_id, record["garment_id"])
        store.put_bytes(key, result.cutout_png, content_type="image/png")

        matted.append(
            {
                **record,
                "cutout_key": key,
                "alpha_coverage": result.alpha_coverage,
                "cutout_width": result.width,
                "cutout_height": result.height,
                "needs_review": needs_review,
                "matte_model": result.model,
            }
        )
        if needs_review:
            logger.warning(
                "job %s garment %s matted to %.2f%% alpha; flagging for review",
                ctx.job_id,
                record["garment_id"],
                result.alpha_coverage * 100,
            )

    if all(r["needs_review"] for r in matted):
        raise Terminal(
            IngestState.NEEDS_REVIEW,
            "every garment matted to almost no subject (dark on dark?); needs a manual crop",
        )

    await _write_cutouts(ctx, matted)
    logger.info(
        "job %s matted %d garment(s): %s",
        ctx.job_id,
        len(matted),
        ", ".join(f"{r['slot_hint']}:{r['alpha_coverage']:.0%}" for r in matted),
    )
    return {"garment_records": matted}


async def _records(ctx: JobContext) -> list[dict[str, Any]]:
    """The garments segmentation found — from scratch, or re-read on a resume.

    Re-reading rather than requiring scratch is what makes a resumed job work:
    the segment stage was skipped, so its in-memory output is gone, but it
    wrote every garment row and its provenance to the database before
    returning.
    """
    cached = ctx.scratch.get("garment_records")
    if cached:
        return list(cached)

    async with tenant_session(ctx.user_id) as session:
        rows = (
            (
                await session.execute(
                    text(
                        """
                    SELECT id, slot, attributes_raw->'segment' AS seg
                    FROM garments
                    WHERE original_key = :orig AND is_active
                    ORDER BY (attributes_raw->'segment'->>'area_pct')::float DESC NULLS LAST
                    """
                    ),
                    {"orig": ctx.payload["key"]},
                )
            )
            .mappings()
            .all()
        )

    if not rows:
        raise Terminal(
            IngestState.REJECTED,
            "no garment rows for this upload; re-ingest the photo",
        )

    return [
        {
            "garment_id": str(row["id"]),
            "is_primary": str(row["id"]) == str(ctx.garment_id),
            "slot_hint": row["slot"],
            "atr_labels": (row["seg"] or {}).get("atr_labels", []),
            "area_pct": (row["seg"] or {}).get("area_pct", 0.0),
            "bbox": (row["seg"] or {}).get("bbox"),
            "mask_key": (row["seg"] or {}).get("mask_key"),
        }
        for row in rows
    ]


async def _write_cutouts(ctx: JobContext, records: list[dict[str, Any]]) -> None:
    """One transaction. The user sees cutouts appear for the whole photo at
    once rather than trickling in mid-write."""
    async with tenant_session(ctx.user_id) as session:
        for record in records:
            await session.execute(
                text(
                    """
                    UPDATE garments
                    SET cutout_key = :cutout,
                        state = :state,
                        needs_review = :needs_review,
                        updated_at = now()
                    WHERE id = :gid
                    """
                ),
                {
                    "cutout": record["cutout_key"],
                    "state": str(IngestState.MATTED),
                    "needs_review": record["needs_review"],
                    "gid": record["garment_id"],
                },
            )


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
    retryable=True,
)
