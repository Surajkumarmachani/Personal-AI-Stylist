"""Stage 8: embed — a 768-d vector per garment (Step 3.4).

Embeds the CUTOUT, not the original. The embedding of a shirt photographed on a
bed encodes the bed, so two different shirts on the same duvet end up closer
together than the same shirt on two different backgrounds — which is exactly
backwards for similarity search and for the style vector in Phase 8.

`embedding_version` is stamped here, NOT `extractor_version`.

They are different models and they version independently: a new embedder means
re-embed and reindex, a new tagger means re-tag. This stage originally wrote
`extractor_version` too, and because embed runs after tag in INGEST_STAGES it
overwrote the tagger's stamp on every ingest — so the column described the
embedder and the tag provenance the correction log depends on was lost. See
migration 0004.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from stylist_db.session import tenant_session
from stylist_worker.state_machine import IngestState, JobContext, Stage

logger = logging.getLogger(__name__)

# Bump when the model, its preprocessing, or the normalisation changes — all
# three move the vectors. Format: <task>-<model>-<revision prefix>.
EXTRACTOR_VERSION = "embed-fashionsiglip-c56244cc"


async def _run(ctx: JobContext) -> dict[str, Any]:
    store = ctx.scratch.get("store") or _default_store()
    ml = ctx.scratch.get("ml") or _default_ml()

    records = await _records(ctx)

    from stylist_clients.ml_client import MLUnavailable
    from stylist_worker.state_machine import Unavailable

    embedded: list[dict[str, Any]] = []
    for record in records:
        cutout = store.get_bytes(record["cutout_key"])
        try:
            result = await ml.embed(image_bytes=cutout)
        except MLUnavailable as exc:
            raise Unavailable(exc.reason, retry_after=exc.retry_after) from exc
        embedded.append({**record, "embedding": list(result.vector), "embed_model": result.model})

    await _write_embeddings(ctx, embedded)
    logger.info("job %s embedded %d garment(s)", ctx.job_id, len(embedded))
    return {"garment_records": embedded}


async def _records(ctx: JobContext) -> list[dict[str, Any]]:
    cached = ctx.scratch.get("garment_records")
    if cached and all(r.get("cutout_key") for r in cached):
        return list(cached)

    async with tenant_session(ctx.user_id) as session:
        rows = (
            (
                await session.execute(
                    text(
                        """
                    SELECT id, slot, cutout_key
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
        {"garment_id": str(r["id"]), "slot_hint": r["slot"], "cutout_key": r["cutout_key"]}
        for r in rows
    ]


async def _write_embeddings(ctx: JobContext, records: list[dict[str, Any]]) -> None:
    async with tenant_session(ctx.user_id) as session:
        for record in records:
            # pgvector accepts its literal text form '[a,b,c]'; passing it as a
            # bound string avoids needing the ORM type in a raw statement.
            literal = "[" + ",".join(f"{v:.7f}" for v in record["embedding"]) + "]"
            await session.execute(
                text(
                    """
                    UPDATE garments
                    SET embedding = CAST(:vec AS vector),
                        embedding_version = :version,
                        state = :state,
                        updated_at = now()
                    WHERE id = :gid
                    """
                ),
                {
                    "vec": literal,
                    "version": EXTRACTOR_VERSION,
                    "state": str(IngestState.EMBEDDED),
                    "gid": record["garment_id"],
                },
            )


def _default_store() -> Any:
    from stylist_worker.deps import get_object_store

    return get_object_store()


def _default_ml() -> Any:
    from stylist_worker.deps import get_ml_client

    return get_ml_client()


embed_stage = Stage(
    name="embed",
    completed_state=IngestState.EMBEDDED,
    run=_run,
    retryable=True,
)
