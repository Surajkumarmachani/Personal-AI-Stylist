"""Stage 4: segment — one photo becomes N garments (Step 3.2).

This is the stage that changes the shape of the pipeline. Until now a job meant
one garment; a mirror selfie legitimately contains three or four, and the whole
promise of one-photo onboarding rests on splitting them.

WHERE THE DECISIONS LIVE
------------------------
The model output comes from `services/stylist_ml`, and every policy decision —
the 2% area floor, IoU de-duplication, merging a pair of shoes, routing drape
ambiguity to review — lives in `stylist_domain.split`, which is pure and
exhaustively unit-tested. This stage is the wiring between them plus the
database writes. Deliberately so: the rules that decide what appears in a
user's wardrobe should be testable in milliseconds without a 112MB model.

GARMENT ROWS ARE CREATED HERE, NOT AT PERSIST
---------------------------------------------
Partial usefulness (View 5): the user should see their items appear, not wait
for the whole chain. The job already owns one garment row from ingest; the
largest mask keeps it — so a single-garment flat-lay behaves exactly as it did
in Phase 2 — and every additional mask gets a new row immediately.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import text

from stylist_db.session import tenant_session
from stylist_domain.split import ComponentInfo, MaskInfo, SplitOutcome, split_masks
from stylist_worker.io_helpers import load_sanitised
from stylist_worker.keys import mask_key
from stylist_worker.state_machine import IngestState, JobContext, Stage, Terminal

logger = logging.getLogger(__name__)


async def _run(ctx: JobContext) -> dict[str, Any]:
    store = ctx.scratch.get("store") or _default_store()
    ml = ctx.scratch.get("ml") or _default_ml()

    image = load_sanitised(ctx, store)

    from stylist_clients.ml_client import MLUnavailable
    from stylist_worker.state_machine import Unavailable

    try:
        seg = await ml.segment(image_bytes=image)
    except MLUnavailable as exc:
        # Backpressure, not a verdict on the image — an ml restart must not
        # burn this photo's retry budget.
        raise Unavailable(exc.reason, retry_after=exc.retry_after) from exc

    decision = split_masks(
        [
            MaskInfo(
                atr_label=m.atr_label,
                slot_hint=m.slot_hint,
                area_pct=m.area_pct,
                bbox=m.bbox,
                index=i,
            )
            for i, m in enumerate(seg.masks)
        ],
        skin_pct=seg.skin_pct,
        components=tuple(
            ComponentInfo(bbox=c.bbox, area_pct=c.area_pct, index=i)
            for i, c in enumerate(seg.flatlay_components)
        ),
    )

    if decision.dropped:
        logger.info("job %s dropped masks: %s", ctx.job_id, "; ".join(decision.dropped))

    if decision.outcome is SplitOutcome.NEEDS_REVIEW:
        # Terminal, and USER-VISIBLE with a reason. The alternative — emitting
        # three garments from one saree — leaves the user hunting down phantoms
        # and concluding the product does not understand their clothes.
        raise Terminal(
            IngestState.NEEDS_REVIEW,
            decision.reason or "segmentation could not identify a garment",
        )

    # The largest garment inherits the job's existing row.
    assert ctx.garment_id is not None
    records: list[dict[str, Any]] = []
    for position, candidate in enumerate(decision.candidates):
        garment_id = ctx.garment_id if position == 0 else uuid.uuid4()

        # A FLAT-LAY CANDIDATE CARRIES NO MASK, and must not.
        #
        # `split_masks` returns one whole-frame candidate with no mask indices
        # when there is no person in the photo, because the human-parsing
        # model's regions are shape guesses there. Writing one of those regions
        # as the mask is what produced the torn cutouts: matting widens its
        # alpha with the mask, so a fragmentary mask punches holes in an
        # otherwise clean matte.
        #
        # `matte` already treats a missing mask as "use rembg's own alpha",
        # which is the correct answer for a flat-lay and demonstrably clean on
        # real photos.
        #
        # A MULTI-GARMENT FLAT-LAY KEEPS THAT INVARIANT. Its candidates carry a
        # connected component's bounding box but still no mask, and `matte`
        # CROPS to that box before matting. So rembg sees a single-garment
        # image and returns its own clean alpha, instead of intersecting it
        # with a segmentation region that may under-cover the fabric — which is
        # the mechanism that tore the cutouts in the first place.
        key: str | None = None
        if candidate.mask_indices:
            mask_png = seg.masks[candidate.mask_indices[0]].mask_png
            if len(candidate.mask_indices) > 1:
                mask_png = _union_masks([seg.masks[i].mask_png for i in candidate.mask_indices])
            key = mask_key(ctx.user_id, garment_id)
            store.put_bytes(key, mask_png, content_type="image/png")
        records.append(
            {
                "garment_id": str(garment_id),
                "is_primary": position == 0,
                "slot_hint": candidate.slot_hint,
                "atr_labels": list(candidate.atr_labels),
                "area_pct": candidate.area_pct,
                "bbox": list(candidate.bbox),
                "mask_key": key,
            }
        )

    await _write_garment_rows(ctx, records, seg, decision.is_worn)

    logger.info(
        "job %s split into %d garment(s): %s (worn=%s)",
        ctx.job_id,
        len(records),
        ", ".join(f"{r['slot_hint']}:{r['area_pct']:.1%}" for r in records),
        decision.is_worn,
    )
    return {
        "garment_records": records,
        "is_worn": decision.is_worn,
        "skin_pct": seg.skin_pct,
        "segment_model": seg.model,
    }


def _union_masks(masks: list[bytes]) -> bytes:
    """OR several masks into one — used for a merged pair of shoes."""
    import io

    import numpy as np
    from PIL import Image

    combined: np.ndarray | None = None
    for raw in masks:
        with Image.open(io.BytesIO(raw)) as img:
            arr = np.asarray(img.convert("L"), dtype=bool)
        combined = arr if combined is None else (combined | arr)

    assert combined is not None
    out = Image.fromarray((combined.astype(np.uint8) * 255), mode="L").convert("1")
    buffer = io.BytesIO()
    out.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


async def _write_garment_rows(
    ctx: JobContext, records: list[dict[str, Any]], seg: Any, is_worn: bool
) -> None:
    """Create/update every garment row in ONE transaction.

    All-or-nothing on purpose: a partial split would leave the wardrobe showing
    two of a photo's three garments with no record that a third was ever found.

    `attributes_raw` carries the segmentation provenance — which ATR classes
    produced this garment, its area, its bbox. That is the §D1 rule about
    storing raw extractor output: a re-derivation later must never need to
    re-run the model.
    """
    async with tenant_session(ctx.user_id) as session:
        for record in records:
            provenance = {
                "segment": {
                    "model": seg.model,
                    "atr_labels": record["atr_labels"],
                    "area_pct": record["area_pct"],
                    "bbox": record["bbox"],
                    "mask_key": record["mask_key"],
                    "slot_hint_confidence": seg.slot_hint_confidence,
                    "is_worn": is_worn,
                    "skin_pct": seg.skin_pct,
                }
            }
            if record["is_primary"]:
                await session.execute(
                    text(
                        """
                        UPDATE garments
                        SET slot = CAST(:slot AS slot),
                            state = :state,
                            attributes_raw = attributes_raw || CAST(:prov AS jsonb),
                            updated_at = now()
                        WHERE id = :gid
                        """
                    ),
                    {
                        "slot": record["slot_hint"],
                        "state": str(IngestState.SEGMENTED),
                        "prov": _json(provenance),
                        "gid": record["garment_id"],
                    },
                )
            else:
                # A garment discovered by the split. Same original photo, so it
                # shares original_key — which is also how the erasure saga and
                # a re-extraction backfill find every garment from one upload.
                # `moderation` is copied from the primary garment, not left
                # empty. The verdict is a property of the PHOTO, and moderate
                # ran before this garment existed — so without the copy a
                # split-discovered garment looks unmoderated forever, and any
                # audit asking "was this image screened?" gets the wrong answer
                # for every garment but the first.
                await session.execute(
                    text(
                        """
                        INSERT INTO garments
                            (id, user_id, original_key, slot, state,
                             attributes_raw, moderation)
                        VALUES
                            (:gid, :uid, :orig, CAST(:slot AS slot), :state,
                             CAST(:prov AS jsonb),
                             COALESCE(
                               (SELECT moderation FROM garments
                                WHERE id = :primary_gid),
                               '{}'::jsonb))
                        """
                    ),
                    {
                        "gid": record["garment_id"],
                        "uid": ctx.user_id,
                        "orig": ctx.payload["key"],
                        "slot": record["slot_hint"],
                        "state": str(IngestState.SEGMENTED),
                        "prov": _json(provenance),
                        "primary_gid": ctx.garment_id,
                    },
                )


def _json(value: Any) -> str:
    import json

    return json.dumps(value)


def _default_store() -> Any:
    from stylist_worker.deps import get_object_store

    return get_object_store()


def _default_ml() -> Any:
    from stylist_worker.deps import get_ml_client

    return get_ml_client()


segment_stage = Stage(
    name="segment",
    completed_state=IngestState.SEGMENTED,
    run=_run,
    retryable=True,
)
