"""Stage 6: classify — colour, locally and for free (Step 3.3).

The stage that makes the two-tier extraction economics work. `primary_colour`
is `tier: local, source: lab_kmeans` in taxonomy.yaml with an eval floor of
0.92 — the second-highest of any field — and it costs no model call, no
provider, no budget. Every field answered here is a field the VLM never has to
be asked about.

WHAT THIS DOES NOT DO
---------------------
`subcategory`, `material`, `formality`, `dress_code`, `warmth` and `fit` are
all `tier: vlm` and arrive in Phase 4. `pattern` is `tier: local,
source: classifier_head` — a logistic head over the FashionSigLIP embedding,
trained on ~2k labelled examples. Those labels come from the golden set, which
does not exist yet, so pattern is deliberately left NULL rather than guessed:
a fabricated pattern label would be indistinguishable from a real one to every
downstream consumer, and `more_than_two_bold_patterns` is a scoring penalty
that would then fire on invented data.

Colour is read from the CUTOUT's non-transparent pixels only. Reading the
original would mix in the bed, the floor and the wall — every garment
photographed on a white duvet would come back partly white.
"""

from __future__ import annotations

import io
import logging
from typing import Any

import numpy as np
from PIL import Image
from sqlalchemy import text

from stylist_db.session import tenant_session
from stylist_domain.colour import read_colours
from stylist_domain.taxonomy import load_taxonomy
from stylist_worker.state_machine import IngestState, JobContext, Stage

logger = logging.getLogger(__name__)

# Alpha above which a pixel counts as garment. Not 0: matting leaves a faint
# halo of very low alpha around the subject, and including it drags every
# reading toward the background colour.
ALPHA_FLOOR = 128

# taxonomy.yaml fields.primary_colour.review_below
COLOUR_REVIEW_BELOW = 0.80


def garment_pixels(cutout_png: bytes) -> np.ndarray:
    """(N, 3) RGB of the garment's own pixels."""
    with Image.open(io.BytesIO(cutout_png)) as img:
        rgba = np.asarray(img.convert("RGBA"))
    opaque = rgba[..., 3] >= ALPHA_FLOOR
    return rgba[opaque][:, :3].astype(np.float64)


async def _run(ctx: JobContext) -> dict[str, Any]:
    store = ctx.scratch.get("store") or _default_store()
    taxonomy = load_taxonomy()
    palette = dict(taxonomy.colour_hex)

    records = await _records(ctx)
    classified: list[dict[str, Any]] = []

    for record in records:
        pixels = garment_pixels(store.get_bytes(record["cutout_key"]))
        if len(pixels) == 0:
            # An empty cutout is a matting failure that slipped the coverage
            # check. Flag it rather than inventing a colour.
            logger.warning("garment %s has no opaque pixels", record["garment_id"])
            classified.append({**record, "colour": None, "needs_review": True})
            continue

        reading = read_colours(pixels, palette)
        classified.append(
            {
                **record,
                "colour": reading,
                "needs_review": record.get("needs_review", False)
                or reading.confidence < COLOUR_REVIEW_BELOW,
            }
        )

    await _write_colours(ctx, classified)
    logger.info(
        "job %s classified %d garment(s): %s",
        ctx.job_id,
        len(classified),
        ", ".join(
            f"{r['slot_hint']}={r['colour'].primary if r['colour'] else '?'}"
            f"({r['colour'].confidence:.2f})"
            if r["colour"]
            else f"{r['slot_hint']}=?"
            for r in classified
        ),
    )
    return {"garment_records": classified}


async def _records(ctx: JobContext) -> list[dict[str, Any]]:
    cached = ctx.scratch.get("garment_records")
    if cached and all(r.get("cutout_key") for r in cached):
        return list(cached)

    # Resume path: re-read from the rows the matte stage wrote.
    async with tenant_session(ctx.user_id) as session:
        rows = (
            (
                await session.execute(
                    text(
                        """
                    SELECT id, slot, cutout_key, needs_review
                    FROM garments
                    WHERE original_key = :orig AND is_active AND cutout_key IS NOT NULL
                    """
                    ),
                    {"orig": ctx.payload["key"]},
                )
            )
            .mappings()
            .all()
        )
    return [
        {
            "garment_id": str(r["id"]),
            "slot_hint": r["slot"],
            "cutout_key": r["cutout_key"],
            "needs_review": r["needs_review"],
        }
        for r in rows
    ]


async def _write_colours(ctx: JobContext, records: list[dict[str, Any]]) -> None:
    async with tenant_session(ctx.user_id) as session:
        for record in records:
            reading = record.get("colour")
            confidence = {"primary_colour": reading.confidence} if reading else {}
            raw = (
                {
                    "classify": {
                        "primary_colour": reading.primary,
                        "primary_share": reading.primary_share,
                        "primary_delta_e": reading.primary_delta_e,
                        "secondary_colour": reading.secondary,
                        "secondary_share": reading.secondary_share,
                        "is_multicolour": reading.is_multicolour,
                        "source": "lab_kmeans",
                    }
                }
                if reading
                else {"classify": {"source": "lab_kmeans", "result": "no_pixels"}}
            )
            await session.execute(
                text(
                    """
                    UPDATE garments
                    SET primary_colour = CAST(:primary AS colour),
                        secondary_colour = CAST(:secondary AS colour),
                        field_confidence = field_confidence || CAST(:conf AS jsonb),
                        attributes_raw = attributes_raw || CAST(:raw AS jsonb),
                        state = :state,
                        needs_review = :needs_review,
                        updated_at = now()
                    WHERE id = :gid
                    """
                ),
                {
                    "primary": reading.primary if reading else None,
                    "secondary": reading.secondary if reading else None,
                    "conf": _json(confidence),
                    "raw": _json(raw),
                    "state": str(IngestState.CLASSIFIED),
                    "needs_review": record["needs_review"],
                    "gid": record["garment_id"],
                },
            )


def _json(value: Any) -> str:
    import json

    return json.dumps(value)


def _default_store() -> Any:
    from stylist_worker.deps import get_object_store

    return get_object_store()


classify_stage = Stage(
    name="classify",
    completed_state=IngestState.CLASSIFIED,
    run=_run,
    # Local and deterministic: no network, no model service. A failure here is
    # a bug in our code or a corrupt cutout, and neither improves on retry.
    retryable=False,
)
