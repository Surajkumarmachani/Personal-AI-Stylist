"""Feedback-replay eval: does a scorer rank history the way the user did?

    python eval/replay_feedback.py
    python eval/replay_feedback.py --user <uuid>

WHAT IT MEASURES
----------------
Pairwise accuracy. For every (preferred, rejected) pair in a user's feedback
history — an outfit they liked or wore against one they disliked, for the same
occasion — did the scorer rank the preferred one higher?

Pairwise, not absolute, because absolute scores are not comparable to a human
reaction. A user liking an outfit does not tell you it should score 0.81; it
tells you it should score ABOVE the one they rejected. Ordering is the only
claim the data supports, so it is the only claim measured.

PAIRS, NOT EVENTS, IS THE DENOMINATOR. A user who only ever taps "like"
produces zero pairs no matter how many events they generate, and a replay over
zero pairs has no accuracy — reporting 100% there is how an eval certifies a
scorer nobody tested. §B1's whole complaint about unmeasurable SLIs is this.

WHAT IT IS FOR
--------------
Phase 11 gates the learned compatibility model on "feedback-replay eval". This
is that eval. It produces a `ReplayResult` per scorer, and
`stylist_domain.promotion.may_promote` decides — the eval measures, it does not
promote.

Today it reports on the deterministic scorer alone, because the learned
challenger is not built (see promotion.py for why not). That is still worth
running: it is the champion's baseline, and a champion with no measured
baseline cannot be beaten by anything in a way you could defend.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO_ROOT / "packages"), str(REPO_ROOT / "services")]

from sqlalchemy import text  # noqa: E402

from stylist_domain.promotion import ReplayResult, may_promote  # noqa: E402

PREFERRED = ("like", "worn")
REJECTED = ("dislike",)


async def _history(session: Any, user_id: uuid.UUID | None) -> list[dict[str, Any]]:
    """Feedback events with the garment tags each outfit was made of."""
    clause = "WHERE f.user_id = :uid" if user_id else ""
    rows = await session.execute(
        text(f"""
            SELECT f.id, f.user_id, f.occasion::text AS occasion, f.kind::text AS kind,
                   f.garment_ids
            FROM outfit_feedback f
            {clause}
            ORDER BY f.created_at
        """),
        {"uid": user_id} if user_id else {},
    )
    return [dict(r) for r in rows.mappings()]


async def _score_deterministic(session: Any, events: list[dict[str, Any]]) -> dict[str, float]:
    """The champion's score for each event's outfit, as it would rank it today."""
    from stylist_domain.scoring import ScoredGarment, score_outfit
    from stylist_domain.style import parse_embedding

    all_ids = sorted({str(g) for e in events for g in e["garment_ids"]})
    if not all_ids:
        return {}
    detail = await session.execute(
        text(
            "SELECT id::text AS id, slot::text AS slot, subcategory::text AS subcategory, "
            "primary_colour::text AS primary_colour, secondary_colour::text AS secondary_colour, "
            "pattern::text AS pattern, material::text AS material, formality, warmth, "
            "embedding::text AS embedding "
            "FROM garments WHERE id = ANY(CAST(:ids AS uuid[]))"
        ),
        {"ids": all_ids},
    )
    by_id = {r["id"]: dict(r) for r in detail.mappings()}

    out: dict[str, float] = {}
    for event in events:
        items = []
        for gid in (str(g) for g in event["garment_ids"]):
            row = by_id.get(gid)
            if row is None:
                continue
            items.append(
                ScoredGarment(
                    garment_id=gid,
                    slot=row["slot"] or "upper_base",
                    subcategory=row["subcategory"] or "unknown",
                    primary_colour=row["primary_colour"],
                    secondary_colour=row["secondary_colour"],
                    pattern=row["pattern"],
                    material=row["material"],
                    formality=row["formality"],
                    warmth=row["warmth"],
                    embedding=tuple(parse_embedding(row["embedding"]) or ()) or None,
                )
            )
        if not items:
            continue
        # Scored WITHOUT the style vector on purpose. The style vector is
        # DERIVED FROM these very reactions, so including it would let the
        # scorer see the answer — a replay that leaks its label measures
        # nothing. Same reason the golden set is not labelled by the tagger.
        result = score_outfit(items, warmth_target=3, formality_target=3)
        out[str(event["id"])] = result.total
    return out


def _pairs(events: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(preferred_event, rejected_event) within the same user and occasion.

    Scoped to one user AND one occasion because a cross-user pair compares two
    people's taste and a cross-occasion pair compares a wedding to a gym trip —
    neither is a statement the scorer is trying to make.
    """
    buckets: dict[tuple[str, str], dict[str, list[str]]] = {}
    for e in events:
        key = (str(e["user_id"]), str(e["occasion"]))
        side = "up" if e["kind"] in PREFERRED else "down" if e["kind"] in REJECTED else None
        if side is None:
            continue
        buckets.setdefault(key, {"up": [], "down": []})[side].append(str(e["id"]))
    out: list[tuple[str, str]] = []
    for group in buckets.values():
        for good in group["up"]:
            for bad in group["down"]:
                out.append((good, bad))
    return out


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default=None, help="restrict to one tenant")
    args = parser.parse_args()

    from stylist_db.session import dispose_engine, init_engine, system_session

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set")
        return 2
    init_engine(dsn)
    try:
        # Owner-level read across tenants when no --user is given. `system_session`
        # returns zero rows for tenant tables, so this is deliberately run with
        # the MIGRATION dsn in CI; with --user it goes through tenant_session.
        from stylist_db.session import tenant_session

        uid = uuid.UUID(args.user) if args.user else None
        if uid is not None:
            async with tenant_session(uid) as session:
                events = await _history(session, uid)
                champion_scores = await _score_deterministic(session, events)
        else:
            async with system_session() as session:
                events = await _history(session, None)
                champion_scores = await _score_deterministic(session, events)
    finally:
        await dispose_engine()

    pairs = _pairs(events)
    correct = sum(
        1
        for good, bad in pairs
        if good in champion_scores
        and bad in champion_scores
        and champion_scores[good] > champion_scores[bad]
    )
    scored_pairs = [p for p in pairs if p[0] in champion_scores and p[1] in champion_scores]

    champion = ReplayResult("deterministic", len(scored_pairs), correct, len(events))
    print(f"events replayed      {champion.events}")
    print(f"ordered pairs        {champion.pairs}")
    print(f"ranked correctly     {champion.correct}")
    print(f"pairwise accuracy    {champion.accuracy:.3f}")

    if champion.pairs == 0:
        print(
            "\nNO ORDERED PAIRS. Every reaction in this history is on the same side,\n"
            "so nothing was ranked against anything. This is not 100% accuracy and\n"
            "it is not 0% — it is an eval with no question in it."
        )

    # The challenger does not exist yet; show what the gate would say.
    challenger = ReplayResult("learned", champion.pairs, champion.correct, champion.events)
    decision = may_promote(champion, challenger)
    print(f"\ngate: promote={decision.promote}")
    print(f"      {decision.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
