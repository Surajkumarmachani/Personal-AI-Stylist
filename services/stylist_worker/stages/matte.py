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

import io
import logging
from typing import Any

from PIL import Image
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

# Breathing room around a connected component before matting it. Components are
# tight against the fabric and rembg needs background to find an edge against.
CROP_PAD_PCT = 0.04

# A box this close to the full frame is the full frame; cropping buys nothing
# and costs a re-encode.
WHOLE_FRAME_AREA_PCT = 0.95

# Below this share of the alpha in one connected blob, the mask SHATTERED the
# garment and the cutout is confetti.
#
# Measured on this wardrobe 2026-09-21:
#
#   white sneakers, masked      55 blobs, largest share 0.56   <- shredded
#   folded jeans, masked        41 blobs, largest share 0.55   <- shredded
#   every cutout that looks ok   1-7 blobs, largest share 0.98-1.00
#   both of the above, UNMASKED  1 blob,   largest share 1.00
#
# 0.80 sits in the empty middle of that gap. The re-matte is what fixes it:
# dropping the mask and cropping to the bounding box turned both shredded
# cases into a single clean blob.
MIN_LARGEST_BLOB_SHARE = 0.80


def _crop_to_bbox(image: bytes, bbox: Any) -> bytes:
    """Crop to `bbox`, or return the image unchanged.

    Unchanged in every doubtful case — a missing box, the whole-frame sentinel
    `(0,0,0,0)` that the single-garment flat-lay path still emits, a box that
    covers essentially the entire frame, or anything unreadable. A crop that
    silently loses part of a garment is worse than no crop, so this only acts
    when the box is unambiguously a sub-region.

    PADDED by CROP_PAD_PCT. Connected components are tight against the fabric,
    and rembg needs some background to find an edge against; a pixel-exact crop
    leaves it deciding where the garment ends with no evidence either side.
    """
    if not bbox or len(bbox) != 4:
        return image
    x1, y1, x2, y2 = (int(v) for v in bbox)
    if x2 <= x1 or y2 <= y1:
        return image
    try:
        img = Image.open(io.BytesIO(image))
        img.load()
    except Exception:  # pragma: no cover - the sanitise stage already validated it
        return image

    w, h = img.size
    if (x2 - x1) * (y2 - y1) >= WHOLE_FRAME_AREA_PCT * w * h:
        # Effectively the whole photo. Cropping would cost a re-encode and
        # change nothing.
        return image

    pad_x, pad_y = int((x2 - x1) * CROP_PAD_PCT), int((y2 - y1) * CROP_PAD_PCT)
    box = (max(0, x1 - pad_x), max(0, y1 - pad_y), min(w, x2 + pad_x), min(h, y2 + pad_y))
    buf = io.BytesIO()
    img.crop(box).save(buf, format="PNG")
    return buf.getvalue()


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
        # A MULTI-GARMENT FLAT-LAY IS CROPPED, NOT MASKED.
        #
        # One photo of four folded garments produces four rows, each with the
        # bounding box of its own connected component and NO mask — because
        # passing a mask here is what tore the cutouts: the matte widens its
        # alpha with the mask, so any region that under-covers the garment
        # punches holes in an otherwise clean result.
        #
        # Cropping sidesteps that entirely. rembg then sees a single-garment
        # image, which is the case it handles cleanly, and the alpha it returns
        # is its own rather than an intersection with a segmentation guess.
        mask_discarded = False
        subject = image
        if mask is None:
            subject = _crop_to_bbox(image, record.get("bbox"))
        try:
            result = await ml.matte(image_bytes=subject, mask_png=mask)

            # A MASK THAT SHATTERS THE GARMENT IS NOT A MASK WORTH KEEPING.
            #
            # segformer's ATR classes are human-parsing labels. With no person
            # in frame they become shape guesses that carve ONE object into
            # several: a single pair of white sneakers came back as Left-shoe
            # 11.9% + Pants 8.4% + Right-shoe 3.6% + Upper-clothes 1.8%, and
            # matting against any one of those gives that label's share of the
            # shoe with everything the other labels claimed punched out. The
            # user's word for the result was "tired"; the cutout was 55
            # disconnected fragments.
            #
            # This is NOT fixable by retuning WORN_SKIN_THRESHOLD, and the
            # measurements are in `largest_blob_share` — the worn and flat-lay
            # skin_pct distributions OVERLAP, so no threshold separates them.
            # Face+Hair and mask solidity fail too.
            #
            # So the decision moves to where the evidence actually is: AFTER
            # the matte, where the damage is visible and measurable. If the
            # mask produced confetti, drop it and re-matte the bounding box
            # with rembg alone — the path flat-lays already take, and the one
            # that turned both measured failures into a single clean blob.
            #
            # Costs a second inference only on the frames that need it.
            if mask is not None and result.largest_blob_share < MIN_LARGEST_BLOB_SHARE:
                logger.warning(
                    "job %s garment %s: mask shattered the cutout "
                    "(largest blob %.0f%% < %.0f%%); re-matting without it",
                    ctx.job_id,
                    record["garment_id"],
                    result.largest_blob_share * 100,
                    MIN_LARGEST_BLOB_SHARE * 100,
                )
                retry = await ml.matte(
                    image_bytes=_crop_to_bbox(image, record.get("bbox")), mask_png=None
                )
                # Keep the retry only if it is actually less fragmented. On a
                # genuinely worn photo the mask is doing real work, and a
                # maskless re-matte there would return the whole outfit — worse
                # than a ragged shirt, and silently so.
                if retry.largest_blob_share > result.largest_blob_share:
                    result = retry
                    mask_discarded = True
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
                # Recorded so "why does this garment have no mask" is
                # answerable later without re-running the pipeline.
                "mask_discarded": mask_discarded,
                "largest_blob_share": result.largest_blob_share,
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
