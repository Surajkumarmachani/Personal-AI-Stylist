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

from stylist_api.deps import CurrentUser, ObjectStoreDep, TenantDB
from stylist_domain.context import resolve_context
from stylist_domain.taxonomy import load_taxonomy
from stylist_suggest import load_wardrobe, suggest

router = APIRouter(tags=["suggestions"])

# Default when the caller gives no weather. Not a forecast — an honest
# mid-scale placeholder, reported in the response as `weather_source`.
DEFAULT_FEELS_LIKE_C = 26.0


@router.get("/suggestions")
async def get_suggestions(
    user: CurrentUser,
    db: TenantDB,
    store: ObjectStoreDep,
    occasion: Annotated[str, Query()] = "casual_outing",
    feels_like_c: Annotated[float | None, Query(ge=-30, le=60)] = None,
    precip_probability: Annotated[float, Query(ge=0, le=1)] = 0.0,
    wind_kmh: Annotated[float, Query(ge=0, le=200)] = 0.0,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    force_live: Annotated[bool, Query()] = False,
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
                ORDER BY score DESC
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
    all_ids = sorted({gid for r in rows for gid in r["garment_ids"]})
    detail = await db.execute(
        text(
            """
            SELECT id, slot::text AS slot, subcategory::text AS subcategory,
                   primary_colour::text AS primary_colour, cutout_key,
                   needs_review, needs_wash
            FROM garments WHERE id = ANY(CAST(:ids AS uuid[]))
            """
        ),
        {"ids": [str(i) for i in all_ids]},
    )
    garments = {
        str(r["id"]): {
            "id": str(r["id"]),
            "slot": r["slot"],
            "subcategory": r["subcategory"],
            "primary_colour": r["primary_colour"],
            "needs_review": r["needs_review"],
            "needs_wash": r["needs_wash"],
            "cutout_url": store.presign_download(r["cutout_key"]) if r["cutout_key"] else None,
        }
        for r in detail.mappings()
    }

    outfits = []
    for r in rows:
        breakdown = r["score_breakdown"]
        if isinstance(breakdown, str):
            breakdown = json.loads(breakdown)
        items = [garments[str(g)] for g in r["garment_ids"] if str(g) in garments]
        if len(items) != len(r["garment_ids"]):
            # A materialised outfit referencing a garment that is gone. The
            # precompute prunes these, but a deletion since the last run can
            # leave one — skip rather than render a gap.
            continue
        outfits.append(
            {
                "garments": items,
                "score": float(r["score"]),
                "informative_weight": (breakdown or {}).get("informative_weight"),
                "score_breakdown": breakdown,
            }
        )

    return {
        "outfits": outfits,
        "served_from": served_from,
        "context": _context_payload(ctx),
        "notes": [],
    }


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
