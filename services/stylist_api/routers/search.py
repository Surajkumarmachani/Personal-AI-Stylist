"""Wardrobe search and filters (Step 5.3).

A DELIBERATE DEVIATION FROM THE PLAN: ts_rank, NOT BM25
-------------------------------------------------------
The plan specifies "BM25 over search_text". Postgres full-text search does not
implement BM25 — `ts_rank_cd` is a coverage-density rank, which weights
proximity and lexeme frequency but has none of BM25's document-length
normalisation or saturating term frequency. Real BM25 needs an extension
(ParadeDB's pg_search) or a separate engine (OpenSearch), and both are a
dependency this phase does not need.

The reason it does not need one is the corpus. A garment's search document is
eight enum values — "kurta top rust cotton solid relaxed festive_ethnic". Every
document is roughly the same length, so length normalisation has nothing to
correct, and no term repeats, so saturating term frequency has nothing to
saturate. The two things BM25 adds over ts_rank are both no-ops on documents
shaped like this.

This is worth revisiting the moment free text enters the document — a user's
own notes, or a description field. Then documents vary in length, terms repeat,
and ts_rank starts favouring whichever garment has the wordiest note.

FILTERS ARE ANDed WITH SEARCH, NOT ORed
---------------------------------------
"red kurta" with slot=top means both. A user who has typed a query and set a
filter has stated two requirements, and returning things that satisfy only one
of them reads as the search being broken.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import text

from stylist_api.deps import CurrentUser, ObjectStoreDep, TenantDB
from stylist_domain.taxonomy import load_taxonomy

router = APIRouter(tags=["search"])


@router.get("/garments/search")
async def search_garments(
    user: CurrentUser,
    db: TenantDB,
    store: ObjectStoreDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    slot: Annotated[str | None, Query()] = None,
    primary_colour: Annotated[str | None, Query()] = None,
    dress_code: Annotated[str | None, Query()] = None,
    climate_band: Annotated[str | None, Query()] = None,
    material: Annotated[str | None, Query()] = None,
    needs_wash: Annotated[bool | None, Query()] = None,
    needs_review: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Full-text + structured filters. Everything is ANDed.

    Enum filters are validated against the taxonomy before they reach SQL: an
    unknown value would otherwise fail as a cast error at the database, which
    surfaces as a 500 on what is really a bad request.
    """
    taxonomy = load_taxonomy()
    allowed: dict[str, tuple[str, ...]] = {
        "slot": taxonomy.slots,
        "primary_colour": taxonomy.colours,
        "dress_code": taxonomy.dress_codes,
        "material": taxonomy.materials,
        "climate_band": taxonomy.climate_bands,
    }
    supplied = {
        "slot": slot,
        "primary_colour": primary_colour,
        "dress_code": dress_code,
        "material": material,
        "climate_band": climate_band,
    }
    for name, value in supplied.items():
        if value is not None and value not in allowed[name]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{value!r} is not a valid {name}; allowed: {sorted(allowed[name])}",
            )

    where = ["g.is_active", "g.state NOT IN ('rejected', 'quarantined')"]
    params: dict[str, Any] = {"lim": limit, "off": offset}

    if q:
        # plainto_tsquery, not to_tsquery: the latter is a query LANGUAGE and
        # raises a syntax error on an unbalanced quote or a bare '&' — which is
        # a 500 caused by the user typing naturally into a search box.
        where.append("g.search_text @@ plainto_tsquery('english', :q)")
        params["q"] = q
    for column, value in (
        ("slot", slot),
        ("primary_colour", primary_colour),
        ("dress_code", dress_code),
        ("material", material),
    ):
        if value is not None:
            where.append(f"g.{column}::text = :{column}")
            params[column] = value
    if climate_band is not None:
        # climate_bands is an array: a garment is wearable in several.
        where.append(":climate_band = ANY(g.climate_bands)")
        params["climate_band"] = climate_band
    if needs_wash is not None:
        where.append("g.needs_wash = :needs_wash")
        params["needs_wash"] = needs_wash
    if needs_review is not None:
        where.append("g.needs_review = :needs_review")
        params["needs_review"] = needs_review

    # Rank only when there is a query. Ordering by ts_rank_cd against an empty
    # tsquery scores every row 0 and produces an arbitrary order that looks
    # like a bug; newest-first is the honest default for "show me everything".
    order = (
        "ts_rank_cd(g.search_text, plainto_tsquery('english', :q)) DESC, g.created_at DESC"
        if q
        else "g.created_at DESC"
    )
    rank_select = (
        "ts_rank_cd(g.search_text, plainto_tsquery('english', :q)) AS rank" if q else "NULL AS rank"
    )

    sql = f"""
        SELECT g.id, g.slot::text AS slot, g.subcategory::text AS subcategory,
               g.primary_colour::text AS primary_colour, g.dress_code::text AS dress_code,
               g.material::text AS material, g.state, g.needs_review, g.needs_wash,
               g.cutout_key, g.duplicate_of, g.created_at, {rank_select}
        FROM garments g
        WHERE {" AND ".join(where)}
        ORDER BY {order}
        LIMIT :lim OFFSET :off
    """
    rows = await db.execute(text(sql), params)
    items = []
    for r in rows.mappings():
        items.append(
            {
                "id": str(r["id"]),
                "slot": r["slot"],
                "subcategory": r["subcategory"],
                "primary_colour": r["primary_colour"],
                "dress_code": r["dress_code"],
                "material": r["material"],
                "state": r["state"],
                "needs_review": r["needs_review"],
                "needs_wash": r["needs_wash"],
                "duplicate_of": str(r["duplicate_of"]) if r["duplicate_of"] else None,
                "cutout_url": store.presign_download(r["cutout_key"]) if r["cutout_key"] else None,
                "rank": float(r["rank"]) if r["rank"] is not None else None,
            }
        )

    total = await db.execute(
        text(f"SELECT count(*) FROM garments g WHERE {' AND '.join(where)}"),
        {k: v for k, v in params.items() if k not in ("lim", "off")},
    )
    return {
        "items": items,
        "count": len(items),
        "total": int(total.scalar_one()),
        "limit": limit,
        "offset": offset,
    }


@router.get("/wardrobe/facets")
async def facets(user: CurrentUser, db: TenantDB) -> dict[str, Any]:
    """Counts per filter value, so the UI offers only filters that match something.

    A filter list built from the taxonomy alone offers 144 subcategories to a
    user with nine garments, and every click but a few returns nothing.

    `subcategory`, `fit` and `pattern` are here for the PREFERENCE FACTS UI
    rather than for search filtering. Same argument, higher stakes: a
    preference picker listing all 144 subcategories invites a user to set a
    rule about a garment type they do not own, which then does nothing and
    teaches them the whole feature is decorative.
    """
    out: dict[str, Any] = {}
    for column in (
        "slot",
        "primary_colour",
        "dress_code",
        "material",
        "subcategory",
        "fit",
        "pattern",
    ):
        rows = await db.execute(
            text(
                f"""
                SELECT {column}::text AS value, count(*) AS n
                FROM garments
                WHERE is_active AND {column} IS NOT NULL
                  AND state NOT IN ('rejected', 'quarantined')
                GROUP BY 1 ORDER BY n DESC, value
                """
            )
        )
        out[column] = [{"value": r["value"], "count": int(r["n"])} for r in rows.mappings()]
    flags = await db.execute(
        text(
            """
            SELECT count(*) FILTER (WHERE needs_wash) AS needs_wash,
                   count(*) FILTER (WHERE needs_review) AS needs_review,
                   count(*) FILTER (WHERE state = 'duplicate_suspect') AS duplicate_suspect,
                   count(*) AS total
            FROM garments
            WHERE is_active AND state NOT IN ('rejected', 'quarantined')
            """
        )
    )
    row = flags.mappings().one()
    out["flags"] = {k: int(v) for k, v in dict(row).items()}
    return out
