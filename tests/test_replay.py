"""The replay harness: turning a feedback log into a grade for the ranker.

`promotion.may_promote` was written, tested, and UNREACHABLE -- nothing in the
codebase produced a `ReplayResult`, so no scorer change could ever be
evaluated and "is this getting better?" had no answer. These cover the half
that was missing.
"""

from stylist_domain.promotion import ReplayResult, may_promote
from stylist_domain.replay import FeedbackEvent, Pair, build_pairs, grade


def _ev(kind: str, ids: tuple[str, ...], *, user: str = "u1", occasion: str = "casual_outing"):
    return FeedbackEvent(user_id=user, occasion=occasion, kind=kind, garment_ids=ids)


def test_a_history_of_only_likes_grades_nothing() -> None:
    """A ranker is graded on ORDER, and order needs two sides.

    Inventing negatives from outfits the user never reacted to would be the
    easy mistake: an unreacted outfit is not a rejection, it may never have
    been seen. Zero pairs is the honest answer, and `ReplayResult.accuracy`
    is 0.0 over zero pairs rather than a flattering 1.0.
    """
    pairs = build_pairs([_ev("like", ("a",)), _ev("worn", ("b",))])
    assert pairs == []
    assert ReplayResult("x", 0, 0, 2).accuracy == 0.0


def test_pairs_are_only_formed_within_one_context() -> None:
    """Liking a sherwani for a wedding and disliking shorts for the gym is not
    a comparison: the two were never candidates for the same question."""
    events = [
        _ev("like", ("a",), occasion="wedding_ceremony"),
        _ev("dislike", ("b",), occasion="workout"),
    ]
    assert build_pairs(events) == []


def test_every_like_is_compared_with_every_dislike() -> None:
    """Two likes and one dislike is three comparisons, not one."""
    events = [_ev("like", ("a",)), _ev("like", ("b",)), _ev("dislike", ("c",))]
    pairs = build_pairs(events)
    assert len(pairs) == 2
    assert {p.preferred for p in pairs} == {("a",), ("b",)}
    assert {p.rejected for p in pairs} == {("c",)}


def test_saved_and_dismissed_are_not_evidence() -> None:
    """The same judgement the bandit makes: saving is intent, a dismissal is
    usually a mis-tap. Grading a scorer on them would grade it on noise."""
    events = [_ev("saved", ("a",)), _ev("dismissed", ("b",))]
    assert build_pairs(events) == []


def test_an_outfit_both_liked_and_disliked_is_dropped() -> None:
    """A contradiction, not a comparison — and not a loss the scorer can win."""
    events = [_ev("like", ("a",)), _ev("dislike", ("a",))]
    assert build_pairs(events) == []


def test_a_tie_is_not_a_win() -> None:
    """A scorer returning a constant must not report 100%."""
    pairs = [Pair("u1", "casual_outing", ("a",), ("b",))]
    correct, graded = grade(pairs, {("a",): 1.0, ("b",): 1.0})
    assert (correct, graded) == (0, 1)


def test_unscoreable_outfits_leave_the_denominator_alone() -> None:
    """A garment that has since been deleted cannot be scored, and guessing
    would put invented evidence into the gate's denominator."""
    pairs = [Pair("u1", "casual_outing", ("a",), ("gone",))]
    assert grade(pairs, {("a",): 1.0}) == (0, 0)


def test_the_gate_refuses_a_challenger_that_merely_ties() -> None:
    """A challenger that wins by nothing has not been shown to be better."""
    champion = ReplayResult("deterministic", 30, 28, 6000)
    challenger = ReplayResult("learned", 30, 28, 6000)
    assert may_promote(champion, challenger).promote is False
