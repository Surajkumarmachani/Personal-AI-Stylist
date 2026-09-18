"""Thompson-sampling bandit over outfit kinds (Phase 11).

WHY A BANDIT WORKS HERE WHEN THE LEARNED MODEL DOES NOT
--------------------------------------------------------
Phase 11 opens with "only now, with >=5k feedback events". That gate is real
for the learned compatibility model: fitted to a few hundred events it
memorises one person's recent choices and reports it as taste, and the
feedback-replay eval meant to catch that is fitted on the same thin data.

A bandit is a different kind of object. It has NO TRAINING PHASE — it starts at
a uniform prior, and with no data every arm draws from Beta(1,1), which is
exactly uniform exploration. It does not need 5k events to be correct; it needs
them to be CONFIDENT, and it reports its own uncertainty by construction.

It is also how the 5k events get collected in a way worth fitting a model to.
A purely greedy ranker only ever shows its own top pick, so the feedback it
gathers says nothing about what it ranked 40th — and a model trained on that
log learns the ranker, not the wearer.

WHAT AN ARM IS
--------------
The DRESS CODE of the outfit. Eight values, so 5k events is ~600 per arm, which
is enough to move a Beta posterior meaningfully. Finer arms — dress code
crossed with colour family, say — are more expressive and need an order of
magnitude more data before they stop being noise; the coarse version is the one
that is useful first, and `arm_key` is the only thing to change.

The bandit learns a per-kind CORRECTION to the deterministic scorer: "this user
likes festive_ethnic more than the weights predict". It does not replace the
scorer, and it cannot invent an outfit the scorer rejected — it only reorders
what the scorer already produced.

DETERMINISTIC WITHIN A DAY, AND THAT IS LOAD-BEARING
-----------------------------------------------------
A bandit is stochastic, and this system requires that the nightly precompute
and the live request path produce the SAME ranking — otherwise the precompute
serves an order the request path would not reproduce, which is the trap
`serve_partial_cache` documents in the reranker.

So the RNG is seeded from (user_id, date). Consequences, all of them wanted:
the list does not reshuffle when the user pulls to refresh, the precompute and
the request path agree exactly, and exploration still happens — across days
rather than across refreshes, which is also the honest cadence for a product
someone opens once a morning.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import date

# Share of slots given to exploration. Phase 11 says 85/15 exploit/explore.
#
# The split applies to SLOTS, not to a coin flip per slot: 15% of a 20-outfit
# list is three exploration slots, every day, rather than a number that happens
# to be zero on some days. A user who only ever sees the top of the list would
# otherwise get no exploration at all on most days.
EXPLORE_RATE = 0.15

# Beta(1,1) — uniform. The honest prior for an arm nobody has reacted to: it
# says "could be anything", which is true, and it makes a cold-start bandit
# explore rather than pick alphabetically.
PRIOR_ALPHA = 1.0
PRIOR_BETA = 1.0


@dataclass(frozen=True, slots=True)
class Arm:
    """One outfit kind and the reactions it has had.

    `successes` and `failures` are counts of feedback events, not a rate, so
    the posterior carries CONFIDENCE as well as a mean — 1 like from 2 shows
    and 500 likes from 1000 have the same rate and must not be treated alike.
    """

    key: str
    successes: int = 0
    failures: int = 0

    def sample(self, rng: random.Random) -> float:
        """One draw from this arm's Beta posterior.

        Thompson sampling in one line: an arm is chosen in proportion to the
        probability that it is the best, which is what sampling the posterior
        gives you for free. No epsilon to tune and no schedule to decay — the
        exploration falls out of the uncertainty as it shrinks.
        """
        return rng.betavariate(PRIOR_ALPHA + self.successes, PRIOR_BETA + self.failures)

    @property
    def shows(self) -> int:
        return self.successes + self.failures


def arm_key(dress_code: str | None) -> str:
    """The arm an outfit belongs to.

    `unknown` for an outfit whose dress code we could not determine, kept as a
    real arm rather than dropped: "outfits we cannot classify" is a meaningful
    bucket and its reaction rate is worth knowing, not least because a low one
    points at the tagger rather than at taste.
    """
    return dress_code or "unknown"


def daily_seed(user_id: str, today: date) -> int:
    """A per-user, per-day seed.

    Hashed rather than concatenated so that two users whose ids differ in the
    last character do not get correlated draws — `random.Random` seeded with
    nearby integers produces nearby first draws, which would make the bandit
    explore in lockstep across a cohort and quietly halve the information it
    collects.
    """
    digest = hashlib.sha256(f"{user_id}:{today.isoformat()}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def explore_slots(count: int, explore_rate: float = EXPLORE_RATE) -> int:
    """How many of `count` slots the bandit will explore.

    Exposed so a caller can REPORT the budget without recomputing it — and so
    the number in the response is provably the number `reorder` used rather
    than a second implementation of the same rounding.
    """
    return round(count * explore_rate)


def reorder(
    ranked: list[tuple[str, str | None]],
    arms: dict[str, Arm],
    *,
    seed: int,
    limit: int | None = None,
    explore_rate: float = EXPLORE_RATE,
) -> list[str]:
    """Interleave exploit and explore picks over an already-ranked list.

    `ranked` is (outfit_id, dress_code) in the deterministic scorer's order —
    best first. A None dress code is a real arm (`unknown`), not an omission:
    see `arm_key`. Returns outfit ids in the order to serve.

    EXPLOIT SLOTS KEEP THE SCORER'S ORDER. The bandit's job is not to re-rank
    everything; a Beta posterior over eight dress codes knows far less about a
    specific outfit than six weighted sub-scores do. It only decides which
    outfits get the exploration slots.

    EXPLORE SLOTS GO TO THE BEST THOMPSON DRAW among candidates not already
    placed, which is what makes this a bandit rather than random exploration:
    a kind the user has liked keeps getting sampled high, a kind they have
    rejected drops away, and a kind nobody has tried stays in contention
    because its posterior is still wide.
    """
    if not ranked:
        return []

    count = len(ranked) if limit is None else min(limit, len(ranked))
    slots = explore_slots(count, explore_rate)
    rng = random.Random(seed)

    # Which positions explore. Spread through the list rather than bunched at
    # the end, so exploration is actually seen — three odd suggestions in
    # positions 18-20 of a 20-item list is exploration the user scrolls past.
    positions = set()
    if slots > 0:
        stride = count / slots
        # Offset by 1 so position 0 is always the scorer's best pick. The top
        # of the list is the product's promise; exploring there spends trust.
        positions = {min(count - 1, int(i * stride) + 1) for i in range(slots)}

    remaining = list(ranked)
    out: list[str] = []
    for position in range(count):
        if not remaining:
            break
        if position in positions and len(remaining) > 1:
            # Thompson: one draw per candidate's arm, take the best.
            draws = [
                (arms.get(arm_key(dc), Arm(arm_key(dc))).sample(rng), idx)
                for idx, (_, dc) in enumerate(remaining)
            ]
            _, pick = max(draws)
        else:
            pick = 0  # the scorer's next best
        out.append(remaining.pop(pick)[0])
    return out


def apply_feedback(arm: Arm, kind: str) -> Arm:
    """Fold one reaction into an arm. Pure, like `style.apply_event`.

    Same reasoning as the style vector: the live update and any replay must be
    one implementation called twice, or the posteriors cannot be rebuilt from
    the event log and become unrebuildable state.

    `dismissed` and `saved` move NEITHER counter. A dismissal is usually "not
    now" or a mis-tap, and counting it as a failure would teach the bandit to
    avoid whatever the user scrolled past; saving is intent, not a verdict.
    Only an explicit like/wear or an explicit dislike is evidence here — the
    style vector can afford a weak signal because it moves a direction, while
    a Beta counter is a claim about probability.
    """
    if kind in ("like", "worn"):
        return Arm(arm.key, arm.successes + 1, arm.failures)
    if kind == "dislike":
        return Arm(arm.key, arm.successes, arm.failures + 1)
    return arm
