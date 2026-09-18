"""The gate a learned scorer must pass before it can rank anything (Phase 11).

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
----------------------------------------------
Phase 11's second item is a learned compatibility model — OutfitTransformer or
MCN, pretrained on Polyvore and fine-tuned on this system's feedback —
replacing `colour_harmony + formality_coherence`, "gated by feedback-replay
eval".

THE MODEL IS NOT BUILT, AND NOT BECAUSE IT WAS SKIPPED. It cannot be: the gate
is >=5k feedback events, there are 3 real ones, and Polyvore pretraining needs
a GPU and a dataset licence this project does not have. Building it anyway
would produce something that, per the plan's own reasoning, "memorises one
person's recent choices and reports it as taste" — and the replay eval meant to
catch that would be fitted on the same thin data.

WHAT IS BUILT IS THE GATE, and the gate is the part that has to exist FIRST.
A learned scorer arriving without one gets promoted because it looked better on
a spreadsheet; a learned scorer arriving into this refuses to rank anything
until it has beaten the deterministic scorer on replayed history by a stated
margin, on a stated number of events. The gate is also the only part that is
testable today, because it is arithmetic over a decision rule rather than a
model.

So this module answers one question — "may the challenger serve?" — and
answers `False` for every reason it should.

WHY A MARGIN AND NOT JUST "BETTER"
-----------------------------------
A challenger that wins by 0.4% on 5,000 events has not been shown to be
better; it has been shown to be indistinguishable. Promoting on a hair's
difference is how a ranking regression ships with a green dashboard behind it.
"""

from __future__ import annotations

from dataclasses import dataclass

# Phase 11's gate, unchanged. It is not arbitrary padding: a compatibility
# model fitted to a few hundred events memorises recent choices, and the eval
# meant to catch that is fitted on the same data.
#
# THE GATE DOES NOT MOVE. Lowering it to fit the data available is the one
# change that makes every number downstream of it meaningless.
MIN_EVENTS_TO_PROMOTE = 5_000

# How much better the challenger must be, in absolute pairwise accuracy.
# 2 points on 5k events is comfortably outside the noise; 0.4 points is not.
MIN_MARGIN = 0.02

# Below this the challenger is worse than guessing on ordered pairs and the
# question of margin does not arise.
MIN_ABSOLUTE_ACCURACY = 0.55


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """How a scorer did on replayed feedback.

    `pairs` is the number of (preferred, rejected) outfit pairs the history
    actually contained — NOT the number of feedback events. A user who only
    ever likes things produces zero pairs, and a replay over zero pairs has an
    accuracy of nothing. Conflating the two is how an eval reports 100% on no
    evidence.
    """

    scorer: str
    pairs: int
    correct: int
    events: int

    @property
    def accuracy(self) -> float:
        """Share of pairs ranked the right way round. 0.0 when there are none —
        not 1.0, and not undefined: a scorer that has been asked nothing has
        got nothing right."""
        return (self.correct / self.pairs) if self.pairs else 0.0


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    promote: bool
    reason: str
    champion_accuracy: float
    challenger_accuracy: float
    margin: float


def may_promote(champion: ReplayResult, challenger: ReplayResult) -> PromotionDecision:
    """May the challenger take over ranking? Pure, and defaults to no.

    Every branch returns a REASON rather than a bare False, because "the
    learned model is not serving" is a state someone will ask about months
    later, and "gate returned false" is not an answer.
    """
    margin = challenger.accuracy - champion.accuracy

    if challenger.events < MIN_EVENTS_TO_PROMOTE:
        return PromotionDecision(
            False,
            f"{challenger.events} feedback event(s), gate is {MIN_EVENTS_TO_PROMOTE}. "
            "Fitted to fewer, a compatibility model memorises recent choices and "
            "reports it as taste",
            champion.accuracy,
            challenger.accuracy,
            margin,
        )

    if challenger.pairs == 0 or champion.pairs == 0:
        return PromotionDecision(
            False,
            "no ordered (preferred, rejected) pairs in the replay — a history of "
            "only likes cannot rank anything, so neither scorer has been tested",
            champion.accuracy,
            challenger.accuracy,
            margin,
        )

    if challenger.pairs != champion.pairs:
        # THE COMPARISON IS THE WHOLE POINT and it is only valid on identical
        # history. Different pair counts mean the two were asked different
        # questions, which is how a "win" gets manufactured by dropping the
        # pairs the challenger finds hard.
        return PromotionDecision(
            False,
            f"replayed on different histories ({champion.pairs} vs "
            f"{challenger.pairs} pairs) — not a comparison",
            champion.accuracy,
            challenger.accuracy,
            margin,
        )

    if challenger.accuracy < MIN_ABSOLUTE_ACCURACY:
        return PromotionDecision(
            False,
            f"challenger accuracy {challenger.accuracy:.3f} is below "
            f"{MIN_ABSOLUTE_ACCURACY} — barely better than guessing on ordered pairs",
            champion.accuracy,
            challenger.accuracy,
            margin,
        )

    if margin < MIN_MARGIN:
        return PromotionDecision(
            False,
            f"margin {margin:+.3f} is under {MIN_MARGIN} — the challenger has been "
            "shown indistinguishable from the deterministic scorer, not better",
            champion.accuracy,
            challenger.accuracy,
            margin,
        )

    return PromotionDecision(
        True,
        f"challenger beats the deterministic scorer by {margin:+.3f} on "
        f"{challenger.pairs} pairs from {challenger.events} events",
        champion.accuracy,
        challenger.accuracy,
        margin,
    )
