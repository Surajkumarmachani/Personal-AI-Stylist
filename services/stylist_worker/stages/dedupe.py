"""Stage 9: dedupe — propose near-duplicates, never merge them (Step 5.1).

TWO SIGNALS, BECAUSE THEY FAIL IN OPPOSITE DIRECTIONS
-----------------------------------------------------
A perceptual hash catches the same PICTURE: re-uploaded, re-encoded, resized.
An embedding catches the same GARMENT photographed again. Either alone is
wrong in a way the other covers, so a match on either proposes a duplicate.
See stylist_domain.phash for why the thresholds are where they are.

THE HASH IS OF THE CUTOUT, NOT THE ORIGINAL
-------------------------------------------
This matters more than it looks. One flat-lay photo can yield three garments,
and all three share an original image — so hashing the original gives three
identical hashes and the stage proposes that every garment in a photo is a
duplicate of its neighbours. The cutout is per garment, so it is the only
image whose hash means "this garment".

IT NEVER MERGES
---------------
A match parks the garment at DUPLICATE_SUSPECT with `duplicate_of` pointing at
the candidate, and asks. The plan is explicit — "ask the user. Never
auto-merge" — and the reason is asymmetry: a wrong merge destroys a garment the
user owns and they may never notice, while a wrong proposal costs one tap. The
pipeline is confident enough to raise the question and never confident enough
to answer it.
"""

from __future__ import annotations

import io
import logging
from typing import Any

from sqlalchemy import text

from stylist_db.session import tenant_session
from stylist_domain.phash import (
    EMBEDDING_DUPLICATE_COSINE,
    dhash,
    is_near_duplicate_hash,
)
from stylist_worker.state_machine import IngestState, JobContext, Stage

logger = logging.getLogger(__name__)


async def _run(ctx: JobContext) -> dict[str, Any]:
    store = ctx.scratch.get("store") or _default_store()
    records = await _records(ctx)

    hashed: list[dict[str, Any]] = []
    for record in records:
        if not record.get("cutout_key"):
            # No cutout means matting degraded. Hashing the original here would
            # be worse than not hashing: see the module docstring.
            hashed.append({**record, "phash": None})
            continue
        from PIL import Image

        image = Image.open(io.BytesIO(store.get_bytes(record["cutout_key"])))
        hashed.append({**record, "phash": dhash(image)})

    proposals = await _find_duplicates(ctx, hashed)
    await _write(ctx, hashed, proposals)

    if proposals:
        logger.info(
            "job %s: %d of %d garment(s) look like duplicates",
            ctx.job_id,
            len(proposals),
            len(hashed),
        )
    return {"garment_records": hashed, "duplicates": proposals}


async def _records(ctx: JobContext) -> list[dict[str, Any]]:
    cached = ctx.scratch.get("garment_records")
    if cached:
        return list(cached)
    async with tenant_session(ctx.user_id) as session:
        rows = await session.execute(
            text(
                """
                SELECT id AS garment_id, cutout_key
                FROM garments
                WHERE original_key = :orig AND is_active
                """
            ),
            {"orig": ctx.payload["key"]},
        )
        return [dict(r) for r in rows.mappings()]


async def _find_duplicates(
    ctx: JobContext, records: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """For each new garment, the best existing candidate — or nothing.

    Scoped to the tenant by RLS, so "duplicate" can only ever mean "duplicate
    of something this user already owns".
    """
    found: dict[str, dict[str, Any]] = {}
    ids = [r["garment_id"] for r in records]
    async with tenant_session(ctx.user_id) as session:
        for record in records:
            # Compare against everything EXCEPT the garments from this same
            # photo. Two garments in one flat-lay are not duplicates of each
            # other, and without this exclusion a photo of two similar shirts
            # flags itself.
            rows = await session.execute(
                text(
                    """
                    SELECT id, phash, subcategory::text AS subcategory,
                           -- CAST on the parameter, not a bare :vec. asyncpg
                           -- infers parameter types from context, and a bare
                           -- placeholder in `IS NOT NULL` has none — it fails
                           -- with "could not determine data type of parameter".
                           CASE WHEN embedding IS NOT NULL
                                     AND CAST(:vec AS text) IS NOT NULL
                                THEN 1 - (embedding <=> CAST(CAST(:vec AS text) AS vector))
                                ELSE NULL END AS cosine
                    FROM garments
                    WHERE is_active
                      AND id <> ALL(CAST(:exclude AS uuid[]))
                      AND state NOT IN ('rejected', 'quarantined', 'duplicate_suspect')
                    ORDER BY cosine DESC NULLS LAST
                    LIMIT 25
                    """
                ),
                {
                    "vec": _vector_literal(record.get("embedding")),
                    "exclude": ids,
                },
            )
            for candidate in rows.mappings():
                by_hash = is_near_duplicate_hash(record.get("phash"), candidate["phash"])
                cosine = candidate["cosine"]
                by_vector = cosine is not None and float(cosine) >= EMBEDDING_DUPLICATE_COSINE
                if by_hash or by_vector:
                    found[str(record["garment_id"])] = {
                        "duplicate_of": str(candidate["id"]),
                        "matched_on": "phash" if by_hash else "embedding",
                        "cosine": float(cosine) if cosine is not None else None,
                    }
                    break
    return found


def _vector_literal(embedding: Any) -> str | None:
    if not embedding:
        return None
    return "[" + ",".join(f"{float(v):.8f}" for v in embedding) + "]"


async def _write(
    ctx: JobContext, records: list[dict[str, Any]], proposals: dict[str, dict[str, Any]]
) -> None:
    async with tenant_session(ctx.user_id) as session:
        for record in records:
            gid = str(record["garment_id"])
            proposal = proposals.get(gid)
            await session.execute(
                text(
                    """
                    UPDATE garments
                    SET phash = :phash,
                        duplicate_of = CAST(:dup AS uuid),
                        -- garments.state is varchar, not a Postgres enum
                        -- (unlike slot/colour/material). No cast.
                        state = :state,
                        updated_at = now()
                    WHERE id = :gid
                    """
                ),
                {
                    "phash": record.get("phash"),
                    "dup": proposal["duplicate_of"] if proposal else None,
                    # DUPLICATE_SUSPECT is terminal for the garment, but the
                    # JOB still completes: the photo was processed correctly,
                    # and there is a question outstanding about one garment in
                    # it. Conflating the two would make a resolved duplicate
                    # require re-ingesting the photo.
                    "state": str(
                        IngestState.DUPLICATE_SUSPECT if proposal else IngestState.DEDUPED
                    ),
                    "gid": record["garment_id"],
                },
            )


def _default_store() -> Any:
    from stylist_worker.deps import get_object_store

    return get_object_store()


dedupe_stage = Stage(
    name="dedupe",
    completed_state=IngestState.DEDUPED,
    run=_run,
    retryable=True,
)
