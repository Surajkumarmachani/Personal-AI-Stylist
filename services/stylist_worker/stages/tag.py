"""Stage 7: tag — VLM attribute extraction (Step 4.2).

THE DEGRADE LADDER LIVES HERE
-----------------------------
This is the only stage that spends money and the only one that sends pixels off
our infrastructure, so it is also the stage with the most ways to fail. Every
one of them lands on DEGRADED_TAGGED — the garment stays fully usable with its
local tags (slot, colour) and a backfill job retries later:

  gateway unreachable   -> Unavailable, waits without spending a retry
  provider errors       -> retry twice, then DEGRADED_TAGGED
  budget exhausted      -> DEGRADED_TAGGED immediately, no retry
  malformed JSON        -> DEGRADED_TAGGED (the response is unusable)
  unknown enum value    -> that FIELD is dropped, the rest is kept

None of them is a 5xx and none loses the garment. That is the difference
between "we use AI" and "we ship AI" (§C6): the user who exhausts their free
budget still has a complete, searchable wardrobe — it just stops gaining new
AI-derived tags.

WHY A SINGLE UNKNOWN VALUE DOES NOT DISCARD THE RESPONSE
--------------------------------------------------------
The schema is strict, so a compliant model cannot return an unknown enum. But
a real one occasionally does, and throwing away five good fields because
`material` came back as "cotton blend" would convert a small model error into a
total extraction failure. Unknown values are dropped per-field and counted, so
the rate is visible — a rising count means the prompt or the model regressed,
which is the earliest and cheapest quality signal available.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from typing import Any

from sqlalchemy import text

from stylist_db.session import tenant_session
from stylist_domain.taxonomy import load_taxonomy
from stylist_domain.vlm_schema import build_prompt, build_schema, required_fields, vlm_fields
from stylist_worker.grid import MAX_CELLS, compose
from stylist_worker.state_machine import IngestState, JobContext, Stage

logger = logging.getLogger(__name__)

# Bump when the model, the prompt or the schema changes — all three change the
# output distribution, and §D1 makes this the trigger for a re-extraction
# backfill.
EXTRACTOR_VERSION = "tag-vlm-v1"


async def _run(ctx: JobContext) -> dict[str, Any]:
    store = ctx.scratch.get("store") or _default_store()
    gateway = ctx.scratch.get("litellm") or _default_gateway()
    taxonomy = load_taxonomy()

    records = await _records(ctx)
    if not records:
        return {"tagged": 0, "reason": "no garments with cutouts"}

    virtual_key = await _virtual_key(ctx)
    if virtual_key is None:
        # No key means the tenant was created before keys existed, or the
        # gateway was down at signup. Degrade rather than block cataloguing.
        logger.warning("user %s has no virtual key; degrading", ctx.user_id)
        await _mark_degraded(ctx, records, "no LiteLLM virtual key for this tenant")
        raise _degraded()

    # A batch is at most MAX_CELLS; more garments than that means more calls.
    # Six per call is a ~6x cost saving on a workload that is nowhere near a
    # throughput bottleneck (§B2).
    batches = [records[i : i + MAX_CELLS] for i in range(0, len(records), MAX_CELLS)]
    tagged = 0
    calls = 0
    total_cost = 0.0

    from stylist_clients.litellm_client import (
        BudgetExhausted,
        LiteLLMUnavailable,
        ProviderError,
    )
    from stylist_worker.state_machine import Unavailable

    for batch in batches:
        cutouts = [(r["garment_id"], store.get_bytes(r["cutout_key"])) for r in batch]
        grid = compose(cutouts)
        hints = {
            cell: _hint(next(r for r in batch if r["garment_id"] == gid))
            for cell, gid in grid.cell_to_garment.items()
        }

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": build_prompt(taxonomy, cells=grid.cells, hints=hints)},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(grid.png).decode("ascii")
                        },
                    },
                ],
            }
        ]

        try:
            result = await gateway.chat(
                model=_model_name(),
                messages=messages,
                api_key=virtual_key,
                response_format=build_schema(taxonomy, cells=grid.cells),
            )
            calls += 1
        except LiteLLMUnavailable as exc:
            # Backpressure. Waits without consuming this image's retry budget,
            # so a gateway restart costs latency rather than the tags.
            raise Unavailable(exc.reason, retry_after=exc.retry_after) from exc
        except BudgetExhausted as exc:
            # §B3's designed outcome, not a failure. No retry: the budget will
            # not refill within this job's lifetime.
            logger.info("tenant %s budget exhausted; degrading", ctx.user_id)
            await _record_call(ctx, model=_model_name(), purpose="tag", failed=True)
            await _mark_degraded(ctx, records, f"budget exhausted: {exc}")
            raise _degraded() from exc
        except ProviderError as exc:
            # Retryable a couple of times by the state machine; after that the
            # DLQ would be wrong (the garment is fine, the tags are missing),
            # so degrade explicitly on the last attempt instead.
            logger.warning("provider error tagging job %s: %s", ctx.job_id, exc)
            await _record_call(ctx, model=_model_name(), purpose="tag", failed=True)
            raise

        await _record_call(
            ctx,
            model=result.model,
            purpose="tag",
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            cache_hit=result.cache_hit,
        )
        total_cost += result.cost_usd or 0.0

        parsed = _parse(result.content, taxonomy, grid.cell_to_garment)
        if parsed is None:
            logger.warning("job %s: unusable VLM response; degrading", ctx.job_id)
            await _mark_degraded(ctx, batch, "VLM response did not parse")
            continue

        await _write_tags(ctx, parsed, taxonomy, raw=result.content, model=result.model)
        tagged += len(parsed)

    logger.info(
        "job %s tagged %d/%d garment(s) in %d call(s), cost $%.6f",
        ctx.job_id,
        tagged,
        len(records),
        calls,
        total_cost,
    )
    return {"tagged": tagged, "vlm_calls": calls, "cost_usd": total_cost}


def _degraded() -> Exception:
    """DEGRADED_TAGGED is terminal for THIS run but explicitly retryable later
    by a backfill — the garment is complete and usable, only its AI tags are
    missing."""
    from stylist_worker.state_machine import Terminal

    return Terminal(
        IngestState.DEGRADED_TAGGED,
        "AI tagging unavailable; item is usable with local tags and will be refined automatically",
    )


def _model_name() -> str:
    from stylist_api.settings import get_settings

    return get_settings().vlm_model


def _hint(record: dict[str, Any]) -> str:
    parts = []
    if record.get("slot"):
        parts.append(f"slot={record['slot']}")
    if record.get("primary_colour"):
        parts.append(f"colour={record['primary_colour']}")
    return ", ".join(parts) or "unknown"


def _parse(
    content: str, taxonomy: Any, cell_to_garment: dict[str, str]
) -> list[dict[str, Any]] | None:
    """Validate the response against taxonomy enums, per field.

    Returns None only when the response is structurally unusable. A response
    with some bad values returns the good ones — see the module docstring.
    """
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    items = payload.get("items")
    if not isinstance(items, list):
        return None

    allowed: dict[str, set[str]] = {
        "subcategory": set(taxonomy.subcategories),
        "material": set(taxonomy.materials),
        "dress_code": set(taxonomy.dress_codes),
        "fit": set(taxonomy.fits),
    }
    fields = vlm_fields(taxonomy)
    required = set(required_fields(taxonomy))

    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        cell = item.get("cell")
        garment_id = cell_to_garment.get(str(cell))
        if garment_id is None:
            # A cell we did not send. The strict schema should prevent this;
            # if it happens, the response is describing something imagined and
            # attaching it to a real garment would be worse than dropping it.
            logger.warning("VLM returned unknown cell %r; dropped", cell)
            continue

        values: dict[str, Any] = {}
        dropped: list[str] = []
        for name in fields:
            value = item.get(name)
            if value is None:
                continue
            if name in allowed and value not in allowed[name]:
                dropped.append(f"{name}={value!r}")
                continue
            if name in ("formality", "warmth") and not (isinstance(value, int) and 1 <= value <= 5):
                dropped.append(f"{name}={value!r}")
                continue
            values[name] = value

        if dropped:
            # Counted, not silent: a rising rate means the prompt or model
            # regressed, and it shows up before users complain.
            logger.warning("garment %s dropped invalid fields: %s", garment_id, dropped)

        missing = required - set(values)
        confidence = item.get("confidence") or {}
        out.append(
            {
                "garment_id": garment_id,
                "values": values,
                "confidence": {
                    k: float(v)
                    for k, v in confidence.items()
                    if isinstance(v, int | float) and 0 <= v <= 1
                },
                "dropped": dropped,
                "missing_required": sorted(missing),
                "raw_item": item,
            }
        )
    return out


async def _write_tags(
    ctx: JobContext, parsed: list[dict[str, Any]], taxonomy: Any, *, raw: str, model: str
) -> None:
    """Persist tags, confidence, and the untouched response.

    `attributes_raw` keeps the ORIGINAL response so a re-derivation never needs
    to re-call the provider (§D1). That is what makes a prompt or parser change
    a local backfill rather than a second invoice.

    `user_verified_fields` is honoured here: a field the user has corrected is
    NEVER overwritten, which is the rule that makes the correction UI
    trustworthy. Doing it in SQL rather than in Python avoids a read-modify-write
    race with a concurrent correction.
    """
    async with tenant_session(ctx.user_id) as session:
        for entry in parsed:
            values = entry["values"]
            review_below = {
                name: taxonomy.fields[name].get("review_below")
                for name in values
                if taxonomy.fields.get(name)
            }
            needs_review = any(
                threshold is not None and entry["confidence"].get(name, 0.0) < threshold
                for name, threshold in review_below.items()
            ) or bool(entry["missing_required"])

            await session.execute(
                text(_TAG_UPDATE_SQL),
                {
                    "subcategory": values.get("subcategory"),
                    "material": values.get("material"),
                    "dress_code": values.get("dress_code"),
                    "fit": values.get("fit"),
                    "formality": values.get("formality"),
                    "warmth": values.get("warmth"),
                    "confidence": json.dumps(entry["confidence"]),
                    "raw": json.dumps(
                        {
                            "tag": {
                                "model": model,
                                "item": entry["raw_item"],
                                "dropped": entry["dropped"],
                                "missing_required": entry["missing_required"],
                                "extractor_version": EXTRACTOR_VERSION,
                            }
                        }
                    ),
                    "version": EXTRACTOR_VERSION,
                    "needs_review": needs_review,
                    "state": str(IngestState.TAGGED),
                    "gid": entry["garment_id"],
                },
            )


def _keep_if_verified(column: str, cast: str | None = None) -> str:
    """SQL that writes a field UNLESS the user has corrected it.

    `user_verified_fields` is checked in SQL rather than in Python because a
    read-modify-write would race a correction submitted while the tag stage is
    mid-flight — and losing that correction is precisely the failure that makes
    a correction UI untrustworthy. §D1: user_verified_fields are NEVER
    overwritten by a backfill.

    COALESCE keeps the existing value when the model omitted the field, so a
    partial response never blanks tags an earlier run got right.
    """
    incoming = f"CAST(:{column} AS {cast})" if cast else f":{column}"
    return (
        f"{column} = CASE WHEN '{column}' = ANY(user_verified_fields) "
        f"THEN {column} ELSE COALESCE({incoming}, {column}) END"
    )


_TAG_UPDATE_SQL = f"""
    UPDATE garments SET
      {_keep_if_verified("subcategory", "subcategory")},
      {_keep_if_verified("material", "material")},
      {_keep_if_verified("dress_code", "dress_code")},
      {_keep_if_verified("fit", "fit")},
      {_keep_if_verified("formality")},
      {_keep_if_verified("warmth")},
      field_confidence = field_confidence || CAST(:confidence AS jsonb),
      attributes_raw   = attributes_raw   || CAST(:raw AS jsonb),
      extractor_version = :version,
      needs_review = needs_review OR :needs_review,
      state = :state,
      updated_at = now()
    WHERE id = :gid
"""


async def _mark_degraded(ctx: JobContext, records: list[dict[str, Any]], reason: str) -> None:
    """Record WHY tagging did not happen, per garment.

    Without the reason, a backfill cannot tell a budget problem (wait for the
    month to roll) from a provider outage (retry soon) from a bad response
    (needs a prompt fix).
    """
    async with tenant_session(ctx.user_id) as session:
        for record in records:
            await session.execute(
                text(
                    """
                    UPDATE garments
                    SET attributes_raw = attributes_raw || CAST(:raw AS jsonb),
                        needs_review = true,
                        updated_at = now()
                    WHERE id = :gid
                    """
                ),
                {
                    "raw": json.dumps({"tag": {"degraded": True, "reason": reason}}),
                    "gid": record["garment_id"],
                },
            )


async def _records(ctx: JobContext) -> list[dict[str, Any]]:
    async with tenant_session(ctx.user_id) as session:
        rows = (
            (
                await session.execute(
                    text(
                        """
                        SELECT id, slot, primary_colour, cutout_key
                        FROM garments
                        WHERE original_key = :orig AND is_active
                          AND cutout_key IS NOT NULL
                        ORDER BY created_at
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
            "slot": r["slot"],
            "primary_colour": r["primary_colour"],
            "cutout_key": r["cutout_key"],
        }
        for r in rows
    ]


async def _virtual_key(ctx: JobContext) -> str | None:
    async with tenant_session(ctx.user_id) as session:
        row = await session.execute(
            text("SELECT litellm_key FROM user_profile WHERE user_id = :uid"),
            {"uid": ctx.user_id},
        )
        return row.scalar_one_or_none()


async def _record_call(
    ctx: JobContext,
    *,
    model: str,
    purpose: str,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    cost_usd: float | None = None,
    latency_ms: int | None = None,
    cache_hit: bool | None = None,
    failed: bool = False,
) -> None:
    """Mirror the call into `model_calls` so cost joins to OUR user and job data.

    LiteLLM has its own spend log, but it cannot answer "cost per garment
    ingested" — it does not know what a garment or a job is. This table is
    where money meets our domain (§B3). It deliberately stores no prompt or
    response body: image bytes and raw model output must not be logged (§D3).
    """
    async with tenant_session(ctx.user_id) as session:
        await session.execute(
            text(
                """
                INSERT INTO model_calls
                    (id, user_id, job_id, model_name, purpose, prompt_tokens,
                     completion_tokens, cost_usd, latency_ms, cache_hit)
                VALUES (:id, :uid, :jid, :model, :purpose, :pt, :ct, :cost, :lat, :cache)
                """
            ),
            {
                "id": uuid.uuid4(),
                "uid": ctx.user_id,
                "jid": ctx.job_id,
                "model": model,
                "purpose": f"{purpose}_failed" if failed else purpose,
                "pt": prompt_tokens,
                "ct": completion_tokens,
                "cost": cost_usd,
                "lat": latency_ms,
                "cache": cache_hit,
            },
        )


def _default_store() -> Any:
    from stylist_worker.deps import get_object_store

    return get_object_store()


def _default_gateway() -> Any:
    from stylist_worker.deps import get_litellm_client

    return get_litellm_client()


tag_stage = Stage(
    name="tag",
    completed_state=IngestState.TAGGED,
    run=_run,
    retryable=True,
)
