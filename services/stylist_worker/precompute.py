"""Step 6.4 — nightly outfit precompute, guarded by a distributed lock.

WHY PRECOMPUTE AT ALL
---------------------
`GET /suggestions` has a p95 budget of 300ms with zero model calls. Generating
500 candidates and scoring each one measures 25-80ms on a 200-item wardrobe
here, which fits — until you multiply it by every user opening the app at 08:00
and remember it re-runs identical work on every refresh. Materialising the top
outfits turns the request path into an indexed read.

ONE EXECUTION ACROSS N REPLICAS
-------------------------------
`pg_try_advisory_lock` on `hashtext('nightly_precompute')`. The plan's test is
explicit: "run three w-cron replicas simultaneously; assert the precompute job
executes exactly once."

try_ rather than the blocking form, deliberately. A replica that cannot get the
lock should log and leave, not queue up behind the winner and run the whole
job again the moment it finishes — which is how you get three sequential runs
instead of one.

The lock is SESSION-scoped, so it is released when the connection closes even
if the process is killed mid-job. That is the property that matters: a crashed
replica must not hold the lock until someone notices.

STALE OUTFITS ARE DELETED, NOT LEFT
-----------------------------------
`outfits.garment_ids` is an array, so a deleted or merged garment cannot be
handled by a foreign key. An outfit referencing a garment that no longer
exists would render as a gap in the UI, so the job removes them explicitly.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date
from typing import Any

from sqlalchemy import text

from stylist_api.settings import get_settings
from stylist_db.session import system_session, tenant_session
from stylist_domain.context import resolve_context
from stylist_domain.scoring import load_scoring_config
from stylist_obs import stage_span
from stylist_suggest import garment_set_hash, load_wardrobe, rerank, suggest
from stylist_suggest.rerank import PRECOMPUTE_TIMEOUT_S

logger = logging.getLogger(__name__)

LOCK_NAME = "nightly_precompute"

# Occasions worth precomputing. NOT all 18: each one is a full generate-and-
# score pass per tenant, and the long tail (workout, funeral) is better served
# on demand than stored nightly for everyone.
PRECOMPUTE_OCCASIONS = ("office_casual", "office_formal", "casual_outing", "dinner_date")

# ~200 per the plan, across all precomputed occasions.
OUTFITS_PER_OCCASION = 50

# Without a stored location we cannot know tomorrow's weather per tenant, so
# precompute covers the middle of the warmth scale and the request path
# recomputes when the actual target differs. Phase 8 stores a home location.
PRECOMPUTE_WARMTH_FALLBACK = 3


async def _acquire(session: Any) -> bool:
    got = await session.execute(
        text("SELECT pg_try_advisory_lock(hashtext(:name))"), {"name": LOCK_NAME}
    )
    return bool(got.scalar_one())


async def _release(session: Any) -> None:
    await session.execute(text("SELECT pg_advisory_unlock(hashtext(:name))"), {"name": LOCK_NAME})


async def _tenants(session: Any) -> list[uuid.UUID]:
    """Every tenant with something to build outfits from.

    Via a SECURITY DEFINER function (migration 0008), NOT a direct select.
    The worker runs as `stylist_app` against FORCE-RLS tables, so
    `SELECT DISTINCT user_id FROM garments` returns zero rows with no error —
    and the job then reports a successful run that precomputed nothing, which
    is indistinguishable from a night when nobody owned any clothes.
    """
    rows = await session.execute(text("SELECT user_id FROM precompute_tenants()"))
    return [r[0] for r in rows]


async def _prune_stale(session: Any, user_id: uuid.UUID) -> int:
    """Drop outfits referencing garments that are gone or unwearable.

    An array column cannot carry a foreign key, so this is the invalidation
    that keeps a merged or deleted garment from rendering as a gap.
    """
    result = await session.execute(
        text(
            """
            DELETE FROM outfits o
            WHERE o.user_id = :uid
              AND EXISTS (
                SELECT 1 FROM unnest(o.garment_ids) AS gid
                WHERE NOT EXISTS (
                    SELECT 1 FROM garments g
                    WHERE g.id = gid AND g.is_active
                      AND g.state NOT IN ('rejected', 'quarantined', 'duplicate_suspect')
                )
              )
            """
        ),
        {"uid": user_id},
    )
    return int(result.rowcount or 0)


async def precompute_for_tenant(
    user_id: uuid.UUID,
    *,
    today: date | None = None,
    warm_rationales: bool = True,
    warm_boards: bool = True,
) -> dict[str, int]:
    """Generate and store outfits for one tenant across the precomputed occasions.

    RATIONALES ARE WARMED HERE, NOT ON THE REQUEST PATH (Phase 7).
    Measured against gemini-3.6-flash, a top-8 rerank takes **3.2-6.4s** —
    against §7.1's 1200ms budget inside §B1's 1500ms SLO. That budget is not
    reachable: even cut to the plan's own token target (1085 in / 370 out, by
    referencing outfits rather than echoing uuids) the floor is ~3.2s, because
    it is generation time and not reasoning overhead.

    So the model call moves off the request. The nightly job pays the 3-6s
    where nobody is waiting, writes rationales into the §7.3 cache, and the
    morning request reads them — the same shape as the outfit precompute this
    function already does, and the reason that cache exists at all.

    `rerank()` reads the cache BEFORE calling, so this is self-limiting: a
    tenant whose wardrobe and outfits are unchanged costs zero provider calls
    until the 7-day TTL expires. That is not a micro-optimisation. Four
    occasions x 30 nights x ~$0.003 is ~$0.36/tenant/month against a
    `free_tier_monthly_budget_usd` of **0.15** — re-calling nightly would blow
    the per-tenant budget by 2.4x and turn a feature into an outage (§B3).
    """
    cfg = load_scoring_config()
    version = int(cfg.get("version", 1))
    written = 0
    pruned = 0
    rationales = 0
    boards = 0

    async with tenant_session(user_id) as session:
        pruned = await _prune_stale(session, user_id)

        for occasion in PRECOMPUTE_OCCASIONS:
            ctx = resolve_context(
                occasion=occasion,
                # A representative temperature, not a real forecast: without a
                # stored location this is the honest placeholder, and the
                # request path recomputes when the real target differs.
                feels_like_c=26.0,
            )
            pool = await load_wardrobe(session, ctx)
            if pool.total == 0:
                continue
            result = suggest(pool, ctx, limit=OUTFITS_PER_OCCASION, today=today)

            for items, score in result.outfits:
                ids = [g.garment_id for g in items]
                await session.execute(
                    text(
                        """
                        INSERT INTO outfits (
                            id, user_id, garment_ids, garment_set_hash, occasion,
                            warmth_target, formality_target, wet, score,
                            score_breakdown, scoring_version
                        ) VALUES (
                            :id, :uid, CAST(:ids AS uuid[]), :hash, :occasion,
                            :warmth, :formality, :wet, :score,
                            CAST(:breakdown AS jsonb), :version
                        )
                        ON CONFLICT (user_id, garment_set_hash, occasion, warmth_target)
                        DO UPDATE SET
                            score = EXCLUDED.score,
                            score_breakdown = EXCLUDED.score_breakdown,
                            scoring_version = EXCLUDED.scoring_version,
                            created_at = now()
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "uid": user_id,
                        "ids": ids,
                        "hash": garment_set_hash(ids),
                        "occasion": occasion,
                        "warmth": ctx.warmth_target,
                        "formality": ctx.formality_target,
                        "wet": ctx.wet,
                        "score": score.total,
                        "breakdown": json.dumps(score.breakdown),
                        "version": version,
                    },
                )
                written += 1

            if warm_rationales:
                rationales += await _warm_rationales(session, user_id, ctx)
            if warm_boards:
                boards += await _warm_boards(session, user_id, ctx)

    return {"written": written, "pruned": pruned, "rationales": rationales, "boards": boards}


# The canonical read order, shared with `GET /suggestions` and
# `GET /outfits/{hash}/board`.
#
# WARMING MUST FOLLOW THE ORDER THE REQUEST WILL USE, AND IT IS NOT THE ORDER
# `suggest()` RETURNS. `suggest()` ranks by an in-memory float; the column is
# `NUMERIC(8,6)`. Two outfits differing at the 7th decimal are DISTINCT in
# Python (so ordered by score) and EQUAL once stored (so ordered by hash), and
# the two sequences diverge. Measured: warming the top-8 of `result.outfits`
# covered only 3 of the 8 the endpoint actually serves, so five of every eight
# requests paid a 629ms cold render for a board the nightly job had already
# rendered under a different hash.
#
# Reading the top-N back through this query makes the two agree by
# construction rather than by two orderings happening to coincide.
TOP_N_SQL = """
    SELECT garment_ids, garment_set_hash
    FROM outfits
    WHERE occasion = :occasion AND warmth_target = :warmth
    ORDER BY score DESC, garment_set_hash
    LIMIT :lim
"""

# The reranker orders 8 (RERANK_TOP_N), so warming more would pay for
# rationales nothing reads.
RATIONALES_PER_OCCASION = 8

# Boards pre-rendered per occasion. The top-8, matching what the reranker
# orders and roughly what a user scrolls before picking — rendering all 50
# would cost 50x the time and storage for outfits nobody scrolls to.
BOARDS_PER_OCCASION = 8


async def _warm_boards(session: Any, user_id: uuid.UUID, ctx: Any) -> int:
    """Render the top boards into object storage. Best effort.

    Measured on the live stack: composing is **45ms** (inside the plan's ~80ms
    budget) but a COLD request is **629ms**, because it also fetches each
    cutout from object storage and writes the PNG back. That is over the
    "< 200ms p95" exit criterion, so — exactly as with rationales in Phase 7 —
    the work moves to where nobody is waiting and the request becomes a lookup
    (measured at 12-20ms warm).

    Self-limiting: a board already in storage is skipped. The key is
    `garment_set_hash` and `compose_board` is deterministic, so an existing
    object is not merely fresh enough, it is byte-identical to what a re-render
    would produce.

    Every failure is swallowed. A tenant whose cutouts are missing still gets
    their outfits precomputed and their suggestions served; they just see a
    board render on first view instead of instantly.
    """
    from stylist_api.routers.boards import board_key
    from stylist_domain.board import compose_board
    from stylist_worker import deps

    store = deps.get_object_store()
    rows = await session.execute(
        text(TOP_N_SQL),
        {"occasion": ctx.occasion, "warmth": ctx.warmth_target, "lim": BOARDS_PER_OCCASION},
    )
    top = [dict(r) for r in rows.mappings()]
    if not top:
        return 0

    # `cutout_key` is READ, never constructed. It looks like
    # `cutouts/{uuid}/{uuid}.png` and it is tempting to rebuild it from the
    # user and garment ids — but the first segment is not always the user
    # (seeded garments share keys across tenants), so a guessed key misses and
    # the board silently never warms.
    wanted = sorted({str(g) for r in top for g in r["garment_ids"]})
    garments = await session.execute(
        text(
            "SELECT id::text AS id, slot::text AS slot, cutout_key FROM garments "
            "WHERE id = ANY(CAST(:ids AS uuid[])) AND is_active"
        ),
        {"ids": wanted},
    )
    meta = {r["id"]: dict(r) for r in garments.mappings()}

    rendered = 0
    for row in top:
        key = board_key(user_id, row["garment_set_hash"])
        ids = [str(g) for g in row["garment_ids"]]
        try:
            if store.head(key) is not None:
                continue
            if any(g not in meta or not meta[g]["cutout_key"] for g in ids):
                continue
            payload = [(g, meta[g]["slot"], store.get_bytes(meta[g]["cutout_key"])) for g in ids]
            store.put_bytes(key, compose_board(payload), content_type="image/png")
            rendered += 1
        except Exception as exc:
            logger.debug("board warm skipped for %s: %s", key, exc)
            continue

    return rendered


async def _warm_rationales(session: Any, user_id: uuid.UUID, ctx: Any) -> int:
    """Rerank this occasion's outfits and cache the rationales. Best effort.

    Reads the top-N through `TOP_N_SQL` — the SAME order the request path uses
    — rather than from `suggest()`'s in-memory ranking. See that constant for
    why the two differ: the stored score is `NUMERIC(8,6)` and the in-memory
    one is a float, so ties form after rounding that do not exist before it.
    Warming a different top-8 than the endpoint reads is how a cache ends up
    permanently half-cold while looking like it is working.

    Every failure here is a no-op, never an exception: a tenant whose reranker
    is down, out of budget, or slow still gets their outfits precomputed, and
    their morning suggestions still work with template rationales. A nightly
    job that abandoned the remaining tenants because one provider call failed
    would be a worse outage than the missing rationales it was trying to fix.
    """
    from stylist_worker import deps

    key_row = await session.execute(text("SELECT litellm_key FROM user_profile LIMIT 1"))
    tenant_key = key_row.scalar()
    if not tenant_key:
        return 0

    rows = await session.execute(
        text(TOP_N_SQL),
        {"occasion": ctx.occasion, "warmth": ctx.warmth_target, "lim": RATIONALES_PER_OCCASION},
    )
    top = [[str(g) for g in r["garment_ids"]] for r in rows.mappings()]
    if not top:
        return 0

    wanted = sorted({g for ids in top for g in ids})
    detail = await session.execute(
        text(
            "SELECT id::text AS id, slot::text AS slot, subcategory::text AS subcategory, "
            "primary_colour::text AS primary_colour, material::text AS material, "
            "formality, warmth FROM garments "
            "WHERE id = ANY(CAST(:ids AS uuid[])) AND is_active"
        ),
        {"ids": wanted},
    )
    meta = {r["id"]: dict(r) for r in detail.mappings()}

    outcome = await rerank(
        [tuple(ids) for ids in top],
        ctx,
        items_by_id={
            gid: {
                "slot": m["slot"],
                "subcategory": m["subcategory"],
                "colour": m["primary_colour"],
                "material": m["material"],
                "formality": m["formality"],
                "warmth": m["warmth"],
            }
            for gid, m in meta.items()
        },
        active_ids=set(meta),
        slots_by_id={gid: (m["slot"], m["subcategory"]) for gid, m in meta.items()},
        gateway=deps.get_litellm_client(),
        api_key=tenant_key,
        model=get_settings().rerank_model,
        cache=deps.get_cache(),
        # 30s, not the request path's 1200ms. Nobody is waiting on this job,
        # and a rerank genuinely takes 3-6s — using the request timeout here
        # would mean the nightly warm always timed out and the cache stayed
        # permanently cold, which is the exact failure this design exists to
        # avoid.
        timeout_s=PRECOMPUTE_TIMEOUT_S,
        # Fill the gaps. The request path stops at the first cache hit; this
        # job must not, or outfits the model skipped on the first run would
        # never acquire a rationale and coverage would freeze there forever.
        serve_partial_cache=False,
    )

    if outcome.source == "deterministic":
        logger.info(
            "rationale warm skipped for %s/%s: %s",
            user_id,
            ctx.occasion,
            outcome.notes[0] if outcome.notes else outcome.reject_rule,
        )
        return 0
    return outcome.cached_writes


async def nightly_precompute(ctx: dict[str, Any]) -> dict[str, Any]:
    """arq cron entrypoint. Exactly one replica does the work."""
    async with system_session() as session:
        if not await _acquire(session):
            # Not an error. Another replica holds it, which is the design.
            logger.info("nightly_precompute: lock held elsewhere, skipping")
            return {"ran": False, "reason": "lock_held"}
        try:
            tenants = await _tenants(session)
        except Exception:
            await _release(session)
            raise

        totals = {"tenants": len(tenants), "written": 0, "pruned": 0, "failed": 0}
        try:
            for user_id in tenants:
                try:
                    with stage_span("precompute.tenant", user_id=user_id):
                        result = await precompute_for_tenant(user_id)
                    totals["written"] += result["written"]
                    totals["pruned"] += result["pruned"]
                except Exception as exc:
                    # One tenant's bad data must not stop the other 19.
                    totals["failed"] += 1
                    logger.warning("precompute failed for %s: %s", user_id, exc)
        finally:
            await _release(session)

    logger.info("nightly_precompute done: %s", totals)
    return {"ran": True, **totals}
