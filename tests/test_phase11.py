"""Phase 11 — bandit, trends, and the gate the learned model has to pass.

WHAT IS TESTED AND WHAT IS NOT
-------------------------------
Phase 11 is three things. Two of them are built and tested here:

  Thompson-sampling bandit   online learner, needs no prior data
  first-party trends         wear velocity, k-anonymity floor, <=10% of score

The third — a learned compatibility model pretrained on Polyvore and
fine-tuned on feedback — IS NOT BUILT, and there is nothing to test because
there is nothing there. The gate it must pass IS built and is tested, because
the gate is arithmetic over a decision rule and the gate is the part that has
to exist before the model, not after.

THE FOURTH THING, which was not in Phase 11 at all: `style_affinity` was dead
from Phase 8 to Phase 11 — a 0.20 weight that returned zero even when handed a
style vector. Those tests live in test_scoring.py next to the sub-score.
"""

from __future__ import annotations

from datetime import date

import pytest

from stylist_domain.bandit import (
    EXPLORE_RATE,
    Arm,
    apply_feedback,
    arm_key,
    daily_seed,
    explore_slots,
    reorder,
)
from stylist_domain.promotion import (
    MIN_EVENTS_TO_PROMOTE,
    MIN_MARGIN,
    PromotionDecision,
    ReplayResult,
    may_promote,
)

RANKED = [(f"o{i}", "casual" if i % 2 else "festive_ethnic") for i in range(20)]
UID = "11111111-1111-1111-1111-111111111111"


# ------------------------------------------------------------------ bandit


def test_the_top_slot_is_never_explored() -> None:
    """The first suggestion is the product's promise. Exploring there spends
    trust for information that positions 2-20 can buy just as well."""
    for day in range(1, 29):
        seed = daily_seed(UID, date(2026, 9, day))
        assert reorder(RANKED, {}, seed=seed)[0] == "o0", f"day {day} explored slot 0"


def test_identical_within_a_day_and_different_across_days() -> None:
    """LOAD-BEARING, not a nicety.

    The nightly precompute and the live request path must produce the same
    ranking, or the precompute serves an order the request path would not
    reproduce — the trap `serve_partial_cache` documents in the reranker. A
    stochastic bandit breaks that unless it is seeded deterministically.

    Seeding from (user, day) also means pull-to-refresh does not reshuffle the
    list, and exploration happens across days, which is the honest cadence for
    something opened once a morning.
    """
    monday = daily_seed(UID, date(2026, 9, 21))
    tuesday = daily_seed(UID, date(2026, 9, 22))
    assert reorder(RANKED, {}, seed=monday) == reorder(RANKED, {}, seed=monday)
    # Across a month, at least some days must differ — otherwise the seed is
    # not varying and there is no exploration at all.
    orders = {
        tuple(reorder(RANKED, {}, seed=daily_seed(UID, date(2026, 9, d)))) for d in range(1, 29)
    }
    assert len(orders) > 1, "the same order every day is not exploration"
    assert tuesday != monday, "consecutive days must not share a seed"


def test_two_users_do_not_explore_in_lockstep() -> None:
    """Seeds are HASHED, not concatenated.

    `random.Random` seeded with nearby integers produces nearby first draws, so
    two users whose ids differ in the last character would explore together and
    the cohort would collect half the information it thinks it does.
    """
    a = "11111111-1111-1111-1111-111111111111"
    b = "11111111-1111-1111-1111-111111111112"
    today = date(2026, 9, 21)
    assert daily_seed(a, today) != daily_seed(b, today)
    # Not merely different — far apart, so the first draws are uncorrelated.
    assert abs(daily_seed(a, today) - daily_seed(b, today)) > 10**6


def test_the_explore_budget_is_the_stated_rate() -> None:
    """85/15 per Phase 11, applied to SLOTS rather than as a coin flip per slot
    — a per-slot flip gives some days no exploration at all."""
    assert pytest.approx(0.15) == EXPLORE_RATE
    assert explore_slots(20) == 3
    assert explore_slots(12) == 2
    # And a list too short to have a spare slot explores nothing rather than
    # spending its only slot.
    assert explore_slots(1) == 0


def test_a_cold_bandit_explores_rather_than_settling() -> None:
    """Beta(1,1) is uniform. An arm nobody has reacted to must stay in
    contention, or a cold start picks whichever arm happens to exist and never
    learns that the other one was better."""
    cold = Arm("never_tried")
    draws = [cold.sample(__import__("random").Random(s)) for s in range(200)]
    assert min(draws) < 0.2 and max(draws) > 0.8, "a uniform prior must span the range"


def test_a_liked_kind_wins_the_explore_slots() -> None:
    """The point of the bandit. A kind the user has liked keeps getting sampled
    high; a kind they have rejected drops away."""
    arms = {
        "festive_ethnic": Arm("festive_ethnic", 200, 5),
        "casual": Arm("casual", 5, 200),
    }
    dress_codes = dict(RANKED)
    seed = daily_seed(UID, date(2026, 9, 21))
    order = reorder(RANKED, arms, seed=seed)
    # Of the outfits promoted OUT of scorer order, the liked kind should
    # dominate. Position 0 is excluded: it is never explored.
    promoted = [o for i, o in enumerate(order[1:6]) if o != RANKED[i + 1][0]]
    assert promoted, "nothing was explored"
    liked = sum(1 for o in promoted if dress_codes[o] == "festive_ethnic")
    assert liked >= len(promoted) / 2, f"the rejected kind won the explore slots: {promoted}"


def test_dismissed_and_saved_move_neither_counter() -> None:
    """A dismissal is usually "not now" or a mis-tap, and counting it as a
    failure teaches the bandit to avoid whatever the user scrolled past.
    Saving is intent, not a verdict.

    The style vector can afford a weak signal because it moves a DIRECTION; a
    Beta counter is a claim about probability and needs evidence.
    """
    base = Arm("casual", 3, 4)
    for kind in ("dismissed", "saved", "unknown_kind"):
        assert apply_feedback(base, kind) == base, kind
    assert apply_feedback(base, "like") == Arm("casual", 4, 4)
    assert apply_feedback(base, "worn") == Arm("casual", 4, 4)
    assert apply_feedback(base, "dislike") == Arm("casual", 3, 5)


def test_an_unknown_dress_code_is_a_real_arm() -> None:
    """Outfits we could not classify are a meaningful bucket, and a low
    reaction rate on it points at the tagger rather than at taste. Dropping
    them would hide that."""
    assert arm_key(None) == "unknown"
    assert arm_key("casual") == "casual"


def test_an_empty_list_reorders_to_empty() -> None:
    assert reorder([], {}, seed=1) == []


def test_every_outfit_survives_the_reorder() -> None:
    """A bandit reorders; it must never drop or duplicate a suggestion."""
    seed = daily_seed(UID, date(2026, 9, 21))
    order = reorder(RANKED, {"casual": Arm("casual", 9, 1)}, seed=seed)
    assert sorted(order) == sorted(o for o, _ in RANKED)


# --------------------------------------------------------- promotion gate


def _result(scorer: str, *, pairs: int, correct: int, events: int) -> ReplayResult:
    return ReplayResult(scorer, pairs, correct, events)


def test_the_5k_gate_does_not_move() -> None:
    """THE NUMBER PHASE 11 IS BUILT AROUND.

    "The 5k gate does not move for n=1... do not compensate by lowering the
    gate." A compatibility model fitted to a few hundred events memorises one
    person's recent choices and reports it as taste, and the replay eval meant
    to catch that is fitted on the same thin data.

    A challenger that is MASSIVELY better on thin data is exactly the case this
    refuses, because that is what overfitting looks like from the inside.
    """
    assert MIN_EVENTS_TO_PROMOTE == 5_000
    champion = _result("deterministic", pairs=100, correct=55, events=400)
    challenger = _result("learned", pairs=100, correct=99, events=400)
    decision = may_promote(champion, challenger)
    assert decision.promote is False
    assert "5000" in decision.reason


def test_an_indistinguishable_challenger_is_not_promoted() -> None:
    """Winning by 0.4% on 5,000 pairs is not being better, it is being
    indistinguishable. Promoting on a hair's difference is how a ranking
    regression ships with a green dashboard behind it."""
    champion = _result("deterministic", pairs=5000, correct=3000, events=5000)
    challenger = _result("learned", pairs=5000, correct=3020, events=5000)
    decision = may_promote(champion, challenger)
    assert decision.promote is False
    assert "indistinguishable" in decision.reason
    assert decision.margin < MIN_MARGIN


def test_a_genuinely_better_challenger_is_promoted() -> None:
    """The gate must be passable, or it is a refusal dressed as a control."""
    champion = _result("deterministic", pairs=5000, correct=3000, events=5000)
    challenger = _result("learned", pairs=5000, correct=3400, events=5000)
    decision = may_promote(champion, challenger)
    assert decision.promote is True, decision.reason
    assert decision.margin == pytest.approx(0.08)


def test_a_replay_on_different_histories_is_not_a_comparison() -> None:
    """Different pair counts mean the two scorers were asked different
    questions — which is how a "win" gets manufactured by quietly dropping the
    pairs the challenger finds hard."""
    champion = _result("deterministic", pairs=5000, correct=3000, events=5000)
    challenger = _result("learned", pairs=4200, correct=3000, events=5000)
    decision = may_promote(champion, challenger)
    assert decision.promote is False
    assert "different histories" in decision.reason


def test_zero_pairs_is_not_a_perfect_score() -> None:
    """A user who only ever taps "like" produces no ordered pairs, however many
    events they generate. Reporting that as 100% is how an eval certifies a
    scorer nobody tested."""
    empty = _result("deterministic", pairs=0, correct=0, events=9000)
    assert empty.accuracy == 0.0, "no questions asked is not all answers right"
    decision = may_promote(empty, _result("learned", pairs=0, correct=0, events=9000))
    assert decision.promote is False
    assert "no ordered" in decision.reason


def test_a_challenger_near_chance_is_refused_before_margin_is_considered() -> None:
    """Barely better than guessing on ordered pairs is not a ranker, even if it
    happens to beat a champion that is also bad."""
    champion = _result("deterministic", pairs=5000, correct=2500, events=5000)
    challenger = _result("learned", pairs=5000, correct=2700, events=5000)
    decision = may_promote(champion, challenger)
    assert decision.promote is False
    assert "guessing" in decision.reason


def test_the_decision_always_carries_a_reason() -> None:
    """ "The learned model is not serving" is a state someone asks about months
    later, and "the gate returned false" is not an answer."""
    cases = [
        (_result("d", pairs=0, correct=0, events=10), _result("l", pairs=0, correct=0, events=10)),
        (
            _result("d", pairs=5000, correct=3000, events=5000),
            _result("l", pairs=5000, correct=3001, events=5000),
        ),
        (
            _result("d", pairs=5000, correct=3000, events=5000),
            _result("l", pairs=5000, correct=4000, events=5000),
        ),
    ]
    for champion, challenger in cases:
        decision = may_promote(champion, challenger)
        assert isinstance(decision, PromotionDecision)
        assert decision.reason and len(decision.reason) > 20


# ------------------------------------------------- trends: the k-anon floor


def test_the_k_anonymity_floor_is_a_privacy_control_not_a_threshold() -> None:
    """A trend computed from two users is a report of what those two users wore
    this fortnight. At n=1 it is the owner's own wardrobe handed back to them
    as a trend — useless, and a privacy claim we should not make.

    Asserted against the constant rather than by running the job, so lowering
    it to make trends appear in a sparse dev database fails here first.
    """
    from stylist_worker.trends import MIN_COHORT_USERS

    assert MIN_COHORT_USERS >= 5, (
        "below 5 a single tenant's behaviour can dominate a published trend row"
    )


def test_trend_scores_are_ranked_within_a_field_not_against_a_constant() -> None:
    """THE BUG THIS REPLACED.

    The first version scored `(velocity - 1) / (SATURATION - 1)` against a
    SATURATION constant of 2.0, and published 156 of 175 rows at exactly 1.0 —
    every value maximally trending, which is a constant, and a constant
    contributes nothing to a ranking. The term was dead again in a new way.

    The deeper problem was the constant: "which absolute velocity counts as
    high" is unanswerable without the data. A trend is comparative, so the
    score is a value's position among the other risers of the same field.
    """
    from stylist_worker.trends import _rank_within_field

    rising = [
        {"f": "subcategory", "v": "kurta", "velocity": 3.0, "u": 9, "wr": 30, "wb": 40},
        {"f": "subcategory", "v": "jeans", "velocity": 1.5, "u": 8, "wr": 20, "wb": 60},
        {"f": "subcategory", "v": "saree", "velocity": 2.0, "u": 7, "wr": 25, "wb": 50},
        {"f": "material", "v": "silk", "velocity": 1.2, "u": 6, "wr": 15, "wb": 60},
    ]
    scored = {(r["f"], r["v"]): r["s"] for r in _rank_within_field(rising)}

    # Spread across the field, not all saturated.
    assert scored[("subcategory", "kurta")] == 1.0
    assert scored[("subcategory", "jeans")] == 0.0
    assert 0.0 < scored[("subcategory", "saree")] < 1.0
    # A field with ONE riser scores 1.0: it is unambiguously that field's
    # trend, and there is nothing to rank it against.
    assert scored[("material", "silk")] == 1.0


def test_unknown_is_never_a_trend() -> None:
    """`unknown` is what `material` and `pattern` hold when tagging could not
    tell. A rising `unknown` is a rising TAGGING GAP, and publishing it would
    reward the scorer for garments we failed to identify."""
    from stylist_worker.trends import _NOT_A_TREND

    assert "unknown" in _NOT_A_TREND


# --------------------------------------------------- rank vs predicted_rank
#
# `/suggestions` stamps `rank` (what the user is shown) and `predicted_rank`
# (where the scorer put it). The UI draws a "shown higher to vary what you
# see" line when predicted > rank, and these tests pin the arithmetic that
# makes that claim honest.


def test_only_the_promoted_outfit_claims_promotion() -> None:
    """Promoting one outfit SHIFTS every outfit after it.

    With a budget of 1, a card lifted from 7th to 2nd pushes the old 2nd..6th
    down one each — so five more cards have `predicted_rank != rank` while
    only ONE was actually explored. A UI that drew its badge from "the numbers
    differ" would announce six promotions for a one-slot budget.

    This is the same artefact the router's own comment records about counting
    positional diffs as `explored_slots`, one layer up. The condition that
    survives it is `predicted > rank`: displacement moves a card DOWN, so its
    predicted rank is smaller, never larger.
    """
    # The permutation the bandit produced: position -> original index.
    order = [0, 6, 1, 2, 3, 4, 5, 7]
    stamped = [
        {"rank": position + 1, "predicted_rank": original + 1}
        for position, original in enumerate(order)
    ]

    differ = [o for o in stamped if o["predicted_rank"] != o["rank"]]
    claims = [o for o in stamped if o["predicted_rank"] > o["rank"]]

    assert len(differ) == 6, "six cards are displaced by promoting one"
    assert len(claims) == 1, "but only one was actually promoted"
    assert claims[0] == {"rank": 2, "predicted_rank": 7}


def test_rank_is_dense_and_one_based() -> None:
    """Off-by-one here mislabels every card, and `ordinal()` would render a
    `0th choice` on the recommendation the user is most likely to read."""
    order = [2, 0, 1]
    ranks = [position + 1 for position, _ in enumerate(order)]
    assert ranks == [1, 2, 3]


def test_an_unshuffled_list_claims_nothing() -> None:
    """When the bandit does not move anything, no card may imply it was
    promoted — `predicted_rank` defaults to the shown rank."""
    stamped = [{"rank": i + 1, "predicted_rank": i + 1} for i in range(5)]
    assert not [o for o in stamped if o["predicted_rank"] > o["rank"]]
