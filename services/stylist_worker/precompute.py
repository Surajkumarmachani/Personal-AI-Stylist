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

from stylist_db.session import system_session, tenant_session
from stylist_domain.context import resolve_context
from stylist_domain.scoring import load_scoring_config
from stylist_obs import stage_span
from stylist_suggest import garment_set_hash, load_wardrobe, suggest

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


async def precompute_for_tenant(user_id: uuid.UUID, *, today: date | None = None) -> dict[str, int]:
    """Generate and store outfits for one tenant across the precomputed occasions."""
    cfg = load_scoring_config()
    version = int(cfg.get("version", 1))
    written = 0
    pruned = 0

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

    return {"written": written, "pruned": pruned}


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
