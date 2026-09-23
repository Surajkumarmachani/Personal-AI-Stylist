"""Turn a feedback history into ordered pairs a scorer can be graded on.

WHAT THIS IS FOR
----------------
`promotion.may_promote` decides whether a challenger scorer may take over
ranking. It takes two `ReplayResult`s and compares them. NOTHING PRODUCED ONE:
the gate existed, fully tested, with no way to be fed — so no scorer change
could ever be evaluated, and "is the recommender getting better?" had no
answer beyond a level metric.

This module is the missing half, and only the pure half: pairing and grading
are arithmetic over an event log, so they are here and unit-testable, while
loading events and scoring outfits needs a database and lives in
`scripts/replay_eval.py`.

WHY PAIRS AND NOT ACCURACY ON EVENTS
-------------------------------------
A ranker is not a classifier. "Did it like this outfit" is unanswerable — the
scorer emits a number, not a verdict, and any threshold turning one into the
other is invented. What a ranker CAN be graded on is order: shown a liked
outfit and a disliked one from the same context, does it put them the right
way round? That question needs no threshold and is exactly the decision the
product makes.

WHY PAIRS ARE SCOPED TO A CONTEXT
----------------------------------
A pair is only evidence if both outfits were plausible answers to the SAME
question. Liking a sherwani for a wedding and disliking shorts for the gym
says nothing about ranking — they were never in competition. Pairs are
therefore formed within one user and one occasion.

WHY A USER WHO ONLY EVER LIKES THINGS YIELDS NOTHING
-----------------------------------------------------
Zero pairs, deliberately. `ReplayResult.accuracy` is 0.0 over zero pairs
rather than 1.0, and this returns an empty list rather than inventing negative
examples from outfits the user never reacted to. An unreacted outfit is not a
rejection; it may simply never have been seen.
"""

from __future__ import annotations

from dataclasses import dataclass

# What counts as evidence of preference, and of rejection.
#
# `saved` is NOT preference and `dismissed` is NOT rejection -- the same
# judgement `bandit.apply_feedback` makes, for the same reason: saving is
# intent and a dismissal is usually a mis-tap. An eval built on weaker
# signals than the bandit trusts would grade a scorer on noise.
PREFERRED: frozenset[str] = frozenset({"like", "worn"})
REJECTED: frozenset[str] = frozenset({"dislike"})


@dataclass(frozen=True, slots=True)
class FeedbackEvent:
    """One reaction, reduced to what grading needs."""

    user_id: str
    occasion: str | None
    kind: str
    garment_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Pair:
    """One ordered comparison: `preferred` should outrank `rejected`."""

    user_id: str
    occasion: str | None
    preferred: tuple[str, ...]
    rejected: tuple[str, ...]


def build_pairs(events: list[FeedbackEvent]) -> list[Pair]:
    """Every (preferred, rejected) combination within a user and occasion.

    The cross product, not a zip: a user who liked two outfits and disliked
    one has made three comparisons, not one. Deterministic order, so a replay
    is reproducible and two runs can be diffed.
    """
    grouped: dict[tuple[str, str | None], dict[str, list[tuple[str, ...]]]] = {}

    for event in events:
        if event.kind not in PREFERRED and event.kind not in REJECTED:
            continue
        ctx = (event.user_id, event.occasion)
        bucket = grouped.setdefault(ctx, {"preferred": [], "rejected": []})
        side = "preferred" if event.kind in PREFERRED else "rejected"
        bucket[side].append(event.garment_ids)

    pairs: list[Pair] = []
    ordered = sorted(grouped.items(), key=lambda kv: (kv[0][0], str(kv[0][1])))
    for (user_id, occasion), bucket in ordered:
        for good in bucket["preferred"]:
            for bad in bucket["rejected"]:
                # The same outfit liked once and disliked once is not a
                # comparison, it is a contradiction. Dropped rather than
                # counted as a loss the scorer cannot win.
                if good == bad:
                    continue
                pairs.append(Pair(user_id, occasion, good, bad))
    return pairs


def grade(pairs: list[Pair], scores: dict[tuple[str, ...], float]) -> tuple[int, int]:
    """How many pairs a scorer ordered correctly. Returns (correct, total).

    A TIE IS NOT A WIN. Two outfits scoring identically means the scorer did
    not separate them, and counting that as correct is how a scorer that
    returns a constant reports 100%.
    """
    correct = 0
    graded = 0
    for pair in pairs:
        good = scores.get(pair.preferred)
        bad = scores.get(pair.rejected)
        if good is None or bad is None:
            # An outfit whose garments are gone cannot be scored, and guessing
            # would put invented evidence into the gate's denominator.
            continue
        graded += 1
        if good > bad:
            correct += 1
    return correct, graded
