"""Grade the scorer against real feedback, and ask the gate if it may ship.

WHAT THIS ANSWERS
-----------------
`GET /me/wear-through` reports a LEVEL: how often a suggestion got worn. It
cannot say whether a change made things better, because it has nothing to
compare against. `promotion.may_promote` exists to answer exactly that and
took two `ReplayResult`s -- which nothing in this codebase produced. The gate
was complete, tested, and unreachable.

This is the missing half: read the feedback log, form ordered pairs, score
both sides with a scorer, and report pairwise accuracy.

HOW TO READ THE RESULT
----------------------
Pairwise accuracy is "shown one outfit the user liked and one they disliked
IN THE SAME CONTEXT, did the scorer rank them the right way round". 0.5 is a
coin flip. It is not a percentage of happy users, and it says nothing about
outfits nobody reacted to.

WHY IT WILL REFUSE TO PROMOTE ANYTHING TODAY
---------------------------------------------
`MIN_EVENTS_TO_PROMOTE` is 5,000 and this database has a handful. That is the
gate working, not a bug: a compatibility model fitted to a few hundred events
memorises recent choices, and an eval built on the same thin data would agree
with it. The number to watch is `pairs`, and it only grows by people using
the product.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

import numpy as np
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from stylist_domain.promotion import ReplayResult, may_promote
from stylist_domain.replay import FeedbackEvent, build_pairs, grade
from stylist_domain.scoring import ScoredGarment, score_outfit

EVENTS_SQL = sa.text(
    """
    SELECT user_id::text AS user_id, occasion, kind::text AS kind,
           ARRAY(SELECT unnest(garment_ids)::text) AS garment_ids
    FROM outfit_feedback
    WHERE created_at > now() - make_interval(days => :days)
    ORDER BY created_at
    """
)

GARMENTS_SQL = sa.text(
    """
    SELECT id::text AS id, slot::text AS slot, subcategory::text AS subcategory,
           primary_colour::text AS primary_colour,
           secondary_colour::text AS secondary_colour,
           pattern::text AS pattern, material::text AS material,
           formality, warmth
    FROM garments WHERE id = ANY(CAST(:ids AS uuid[]))
    """
)


def _garment(row: dict[str, Any]) -> ScoredGarment:
    return ScoredGarment(
        garment_id=row["id"],
        slot=row["slot"],
        subcategory=row["subcategory"],
        primary_colour=row["primary_colour"],
        secondary_colour=row["secondary_colour"],
        pattern=row["pattern"],
        material=row["material"],
        formality=row["formality"] or 3,
        warmth=row["warmth"] or 3,
        wear_count=0,
        last_worn=None,
        embedding=None,
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument(
        "--style-events",
        type=int,
        default=0,
        help="pretend the style vector has this many events, to grade the "
        "personalised scorer against the unpersonalised one",
    )
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set")
        return 1

    engine = create_async_engine(dsn)
    async with engine.begin() as conn:
        rows = (await conn.execute(EVENTS_SQL, {"days": args.days})).mappings().all()
        events = [
            FeedbackEvent(
                user_id=r["user_id"],
                occasion=r["occasion"],
                kind=r["kind"],
                garment_ids=tuple(sorted(r["garment_ids"])),
            )
            for r in rows
        ]
        pairs = build_pairs(events)
        wanted = {gid for p in pairs for gid in (*p.preferred, *p.rejected)}
        detail: dict[str, dict[str, Any]] = {}
        if wanted:
            got = (
                await conn.execute(GARMENTS_SQL, {"ids": sorted(wanted)})
            ).mappings().all()
            detail = {r["id"]: dict(r) for r in got}
    await engine.dispose()

    print(f"feedback events in window : {len(events)}")
    print(f"ordered pairs formed      : {len(pairs)}")
    if not pairs:
        print("\nNo pairs. A history with no dislikes cannot grade a ranker --")
        print("liking everything says nothing about ORDER. See replay.build_pairs.")
        return 0

    def score_set(ids: tuple[str, ...], style_events: int) -> float | None:
        garments = [_garment(detail[g]) for g in ids if g in detail]
        if len(garments) != len(ids):
            return None
        vec = np.zeros(8, dtype=float) if style_events else None
        return score_outfit(
            garments,
            warmth_target=3,
            formality_target=3,
            style_vector=vec,
            style_events=style_events,
        ).total

    champion_scores = {
        ids: s
        for ids in {p.preferred for p in pairs} | {p.rejected for p in pairs}
        if (s := score_set(ids, 0)) is not None
    }
    correct, graded = grade(pairs, champion_scores)
    champion = ReplayResult("deterministic", graded, correct, len(events))
    print(f"\nchampion  {champion.scorer:16} pairs={champion.pairs} "
          f"correct={champion.correct} accuracy={champion.accuracy:.3f}")

    if args.style_events:
        challenger_scores = {
            ids: s
            for ids in {p.preferred for p in pairs} | {p.rejected for p in pairs}
            if (s := score_set(ids, args.style_events)) is not None
        }
        c_correct, c_graded = grade(pairs, challenger_scores)
        challenger = ReplayResult("personalised", c_graded, c_correct, len(events))
        print(f"challenger {challenger.scorer:15} pairs={challenger.pairs} "
              f"correct={challenger.correct} accuracy={challenger.accuracy:.3f}")
        decision = may_promote(champion, challenger)
        print(f"\npromote: {decision.promote}  ({decision.reason})")
        print(f"margin : {decision.margin:+.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
