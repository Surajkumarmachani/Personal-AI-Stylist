"""GET /suggestions — ranked outfits, zero model calls (Phase 6 exit criterion).

TWO PATHS, AND THE FAST ONE IS THE DEFAULT
------------------------------------------
    materialised  read the nightly precompute. An indexed read; this is what
                  meets the p95 < 300ms budget.
    on demand     generate and score live. Used when the request's context
                  does not match anything precomputed — a different occasion,
                  or weather that moved the warmth target.

The fallback is not a nicety. Precompute cannot cover 18 occasions x 5 warmth
levels x wet/dry per tenant, and a user who picks "interview" on a cold day
must not get an empty screen because nobody precomputed that combination.

WHY THE RESPONSE CARRIES `informative_weight`
---------------------------------------------
Three of the six sub-scores currently have no signal — formality, warmth and
(for an unworn wardrobe) novelty all come from tags that are either mock or
unset. A ranked list with no caveat invites the reader to trust an ordering
that is 30-65% arbitrary. The number is computed per outfit, not asserted here,
so it stops being a caveat on its own when real tags arrive.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import text

from stylist_api.deps import (
    CacheRedisDep,
    CurrentUser,
    LiteLLMDep,
    ObjectStoreDep,
    SettingsDep,
    TenantDB,
)
from stylist_domain.context import resolve_context
from stylist_domain.taxonomy import load_taxonomy
from stylist_suggest import TEMPLATE_RATIONALE, load_wardrobe, rerank, suggest

router = APIRouter(tags=["suggestions"])

# Default when the caller gives no weather. Not a forecast — an honest
# mid-scale placeholder, reported in the response as `weather_source`.
DEFAULT_FEELS_LIKE_C = 26.0


@router.get("/suggestions")
async def get_suggestions(
    user: CurrentUser,
    db: TenantDB,
    store: ObjectStoreDep,
    settings: SettingsDep,
    gateway: LiteLLMDep,
    cache: CacheRedisDep,
    occasion: Annotated[str, Query()] = "casual_outing",
    feels_like_c: Annotated[float | None, Query(ge=-30, le=60)] = None,
    precip_probability: Annotated[float, Query(ge=0, le=1)] = 0.0,
    wind_kmh: Annotated[float, Query(ge=0, le=200)] = 0.0,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    force_live: Annotated[bool, Query()] = False,
    # Phase 7. ON by default — the reranker is part of `/suggestions` now, and
    # its exit criterion is a p95 on this endpoint. `rerank=false` preserves
    # the Phase 6 path exactly (deterministic, ZERO model calls), which is what
    # that phase's 7.5ms/58ms numbers were measured on and how they stay
    # re-measurable rather than becoming history.
    rerank_enabled: Annotated[bool, Query(alias="rerank")] = True,
) -> dict[str, Any]:
    taxonomy = load_taxonomy()
    if occasion not in {o["id"] for o in taxonomy.raw["occasions"]}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown occasion; taxonomy defines {sorted(taxonomy.occasions)}",
        )

    ctx = resolve_context(
        occasion=occasion,
        feels_like_c=DEFAULT_FEELS_LIKE_C if feels_like_c is None else feels_like_c,
        precip_probability=precip_probability,
        wind_kmh=wind_kmh,
    )

    served_from = "materialised"
    rows: list[dict[str, Any]] = []

    if not force_live:
        result = await db.execute(
            text(
                """
                SELECT garment_ids, score, score_breakdown
                FROM outfits
                WHERE occasion = :occasion AND warmth_target = :warmth
                -- THE TIE-BREAK IS NOT COSMETIC. `suggest()` sorts by
                -- (-score, garment_set_hash) precisely so that two outfits
                -- with the same score rank identically across runs; this
                -- read path had only `score DESC`, so Postgres returned an
                -- ARBITRARY set among ties.
                --
                -- On real data scores tie constantly (five outfits at 0.717
                -- here), so the nightly job reranked one arbitrary top-8 and
                -- the request read a different arbitrary top-5. Every
                -- rationale-cache lookup missed, the endpoint fell through to
                -- a live call it cannot afford, and §7.3's "hit rate >= 50%"
                -- would have been unreachable for a reason no dashboard
                -- could show. Phase 6 wrote this rule down in `suggest()`;
                -- the SQL that reads its output did not follow it.
                ORDER BY score DESC, garment_set_hash
                LIMIT :lim
                """
            ),
            {"occasion": occasion, "warmth": ctx.warmth_target, "lim": limit},
        )
        rows = [dict(r) for r in result.mappings()]

    if not rows:
        # Nothing precomputed for this context. Generate live rather than
        # returning an empty list — see the module docstring.
        served_from = "live"
        pool = await load_wardrobe(db, ctx)
        live = suggest(pool, ctx, limit=limit)
        rows = [
            {
                "garment_ids": [g.garment_id for g in items],
                "score": score.total,
                "score_breakdown": score.breakdown,
            }
            for items, score in live.outfits
        ]
        if not rows:
            return {
                "outfits": [],
                "served_from": served_from,
                "context": _context_payload(ctx),
                # WHY it is empty, not just that it is. "No suggestions" with
                # no reason is the least actionable screen in the product.
                "notes": pool.notes
                or ["no outfit satisfied the slot rules from the wearable pool"],
                "candidates_considered": live.candidates_considered,
            }

    # Hydrate the garments in ONE query rather than per outfit. Ten outfits of
    # four garments is 40 ids and would otherwise be 40 round trips.
    outfits, rows_by_id = await _hydrate(db, store, rows)

    # PREFERENCE FILTERING CAN EMPTY THE MATERIALISED SET, and then the user
    # gets a blank screen for having set a rule. Measured: a `never white`
    # dropped all ten precomputed outfits, because the hydration predicate
    # removes the garment and the loop then skips the whole outfit.
    #
    # This is what the live path already exists for — "a user who picks
    # interview on a cold day must not get an empty screen" — it was simply
    # checked before filtering rather than after. Regenerating honours the
    # rules at generation time, so the result is non-empty AND obeys them.
    if not outfits and served_from == "materialised":
        served_from = "live"
        pool = await load_wardrobe(db, ctx)
        live = suggest(pool, ctx, limit=limit)
        rows = [
            {
                "garment_ids": [g.garment_id for g in items],
                "score": score.total,
                "score_breakdown": score.breakdown,
            }
            for items, score in live.outfits
        ]
        outfits, rows_by_id = await _hydrate(db, store, rows)
        if not outfits:
            return {
                "outfits": [],
                "served_from": served_from,
                "context": _context_payload(ctx),
                "notes": pool.notes
                or ["no outfit satisfied your preferences and today's slot rules"],
            }

    ranking_source = "deterministic"
    reject_rule: str | None = None
    rerank_notes: list[str] = []

    if rerank_enabled and outfits:
        # WHAT LEAVES THE BUILDING, decided here and not in the prompt builder.
        # Tags only — no ids beyond the opaque uuid, no colours-of-a-person, no
        # image. Keeping this projection at the call site is deliberate: it is
        # a privacy decision, and a privacy decision buried three modules down
        # is one nobody reviews.
        items_by_id = {
            gid: {
                "slot": r["slot"],
                "subcategory": r["subcategory"],
                "colour": r["primary_colour"],
                "material": r["material"],
                "formality": r["formality"],
                "warmth": r["warmth"],
            }
            for gid, r in rows_by_id.items()
        }
        order_in = [tuple(str(g) for g in o["_ids"]) for o in outfits]

        # The tenant's own virtual key, never the master key — the reranker is
        # a paid call and must land on the budget that belongs to whoever asked
        # for it (§B3).
        key_row = await db.execute(text("SELECT litellm_key FROM user_profile LIMIT 1"))
        tenant_key = key_row.scalar()

        outcome = await rerank(
            order_in,
            ctx,
            items_by_id=items_by_id,
            active_ids=set(rows_by_id),
            slots_by_id={gid: (r["slot"], r["subcategory"]) for gid, r in rows_by_id.items()},
            gateway=gateway if tenant_key else None,
            api_key=tenant_key,
            model=settings.rerank_model,
            cache=cache,
            min_confidence=settings.rerank_min_confidence,
        )

        by_ids = {tuple(str(g) for g in o["_ids"]): o for o in outfits}
        outfits = [by_ids[ids] for ids in outcome.order if ids in by_ids]
        for o in outfits:
            o["rationale"] = outcome.rationales.get(
                tuple(str(g) for g in o["_ids"]), TEMPLATE_RATIONALE
            )
        ranking_source = outcome.source
        reject_rule = outcome.reject_rule
        rerank_notes = outcome.notes
    else:
        for o in outfits:
            o["rationale"] = TEMPLATE_RATIONALE

    for o in outfits:
        o.pop("_ids", None)

    return {
        "outfits": outfits,
        "served_from": served_from,
        # HOW the list was ordered, surfaced rather than logged. A ranking that
        # silently switches between a model and a scorer is unexplainable when
        # someone asks why this morning's suggestion is different.
        "ranking_source": ranking_source,
        "validator_reject_rule": reject_rule,
        "context": _context_payload(ctx),
        "notes": rerank_notes,
    }


async def _hydrate(
    db: Any, store: Any, rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Rows -> renderable outfits, in one query.

    The `never` predicate lives here rather than in the caller so BOTH the
    materialised and the live path go through it. `load_wardrobe` already
    filters the live pool, but routing both through one hydration keeps the two
    from drifting — a rule enforced on one path and not the other is worse than
    one enforced on neither, because it looks like it works.
    """
    if not rows:
        return [], {}

    all_ids = sorted({gid for r in rows for gid in r["garment_ids"]})
    detail = await db.execute(
        text(
            """
            SELECT id, slot::text AS slot, subcategory::text AS subcategory,
                   primary_colour::text AS primary_colour, material::text AS material,
                   formality, warmth, cutout_key, needs_review, needs_wash
            FROM garments g
            WHERE g.id = ANY(CAST(:ids AS uuid[])) AND g.is_active
              AND NOT EXISTS (
                SELECT 1 FROM preference_fact pf
                WHERE pf.kind = 'never'
                  AND (
                       (pf.field_name = 'subcategory' AND pf.field_value = g.subcategory::text)
                    OR (pf.field_name = 'primary_colour'
                        AND pf.field_value = g.primary_colour::text)
                    OR (pf.field_name = 'material' AND pf.field_value = g.material::text)
                    OR (pf.field_name = 'fit'      AND pf.field_value = g.fit::text)
                    OR (pf.field_name = 'pattern'  AND pf.field_value = g.pattern::text)
                  )
              )
            """
        ),
        {"ids": [str(i) for i in all_ids]},
    )
    rows_by_id = {str(r["id"]): dict(r) for r in detail.mappings()}
    garments = {
        gid: {
            "id": gid,
            "slot": r["slot"],
            "subcategory": r["subcategory"],
            "primary_colour": r["primary_colour"],
            "needs_review": r["needs_review"],
            "needs_wash": r["needs_wash"],
            "cutout_url": store.presign_download(r["cutout_key"]) if r["cutout_key"] else None,
        }
        for gid, r in rows_by_id.items()
    }

    outfits: list[dict[str, Any]] = []
    for r in rows:
        breakdown = r["score_breakdown"]
        if isinstance(breakdown, str):
            breakdown = json.loads(breakdown)
        items = [garments[str(g)] for g in r["garment_ids"] if str(g) in garments]
        if len(items) != len(r["garment_ids"]):
            # A garment that is gone, or one a `never` rule just removed.
            # Either way the outfit cannot be rendered honestly, so it is
            # skipped rather than shown with a gap.
            continue
        outfits.append(
            {
                "_ids": [str(g) for g in r["garment_ids"]],
                "garments": items,
                "score": float(r["score"]),
                "informative_weight": (breakdown or {}).get("informative_weight"),
                "score_breakdown": breakdown,
            }
        )
    return outfits, rows_by_id


def _context_payload(ctx: Any) -> dict[str, Any]:
    return {
        "occasion": ctx.occasion,
        "warmth_target": ctx.warmth_target,
        "formality_target": ctx.formality_target,
        "dress_code_target": ctx.dress_code_target,
        "wet": ctx.wet,
        "wind_adjusted": ctx.wind_adjusted,
        "feels_like_c": ctx.feels_like_c,
    }
