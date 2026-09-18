"""Step 6.3 — the deterministic scorer.

Each sub-score is tested on its own, as the plan requires. The two properties
that matter most across all of them:

  - every sub-score stays inside 0..1, or the weights stop meaning what they
    say and the total becomes uninterpretable;
  - a sub-score whose input carries no signal says so, because otherwise a bad
    ranking is unattributable — you cannot tell a broken scorer from uniform
    input, which is the single most expensive kind of ambiguity in this project.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from stylist_domain.scoring import (
    ScoredGarment,
    colour_harmony,
    formality_coherence,
    hard_penalty,
    load_scoring_config,
    novelty,
    score_outfit,
    style_affinity,
    trend_alignment,
    weather_fit,
)
from stylist_domain.taxonomy import load_taxonomy

TODAY = date(2026, 9, 11)


def g(slot: str, sub: str, **kw: object) -> ScoredGarment:
    return ScoredGarment(garment_id=f"{slot}:{sub}", slot=slot, subcategory=sub, **kw)  # type: ignore[arg-type]


def outfit(**kw: object) -> list[ScoredGarment]:
    """A valid base outfit, so rule violations do not mask sub-score tests."""
    return [
        g("upper_base", "shirt_oxford", **kw),
        g("lower", "chinos", **kw),
        g("feet", "loafers", **kw),
    ]


# ------------------------------------------------------------ config


def test_weights_must_sum_to_one() -> None:
    """A total that is not on a 0..1 scale cannot be compared or explained."""
    cfg = load_scoring_config()
    assert abs(sum(float(v) for v in cfg["weights"].values()) - 1.0) < 1e-6


def test_weights_match_the_plan() -> None:
    w = load_scoring_config()["weights"]
    assert w["colour_harmony"] == 0.30
    assert w["formality_coherence"] == 0.20
    assert w["weather_fit"] == 0.15
    assert w["style_affinity"] == 0.20
    assert w["novelty"] == 0.10
    assert w["trend_alignment"] == 0.05


def test_the_neutral_list_is_not_duplicated_in_scoring_config() -> None:
    """Colour families live in taxonomy.yaml only.

    A hand-copied list in a second file is exactly how the bold-pattern set
    drifted and silently under-penalised festive combinations.
    """
    cfg = load_scoring_config()["colour_harmony"]
    assert "neutral_families" not in cfg
    assert cfg["neutral_family_name"] == "neutral"


# ------------------------------------------------------ colour harmony


def test_neutrals_never_clash() -> None:
    """Black, white and grey together is a normal outfit, not a collision."""
    items = [
        g("upper_base", "shirt_formal", primary_colour="white"),
        g("lower", "trousers_formal", primary_colour="black"),
        g("feet", "loafers", primary_colour="grey_charcoal"),
    ]
    assert colour_harmony(items).value == 1.0


def test_one_chromatic_colour_against_neutrals_scores_top() -> None:
    items = [
        g("upper_base", "kurta", primary_colour="rust"),
        g("lower", "churidar", primary_colour="white"),
        g("feet", "juttis", primary_colour="black"),
    ]
    result = colour_harmony(items)
    assert result.value == 1.0
    assert result.informative is True


def test_missing_colours_are_reported_as_uninformative() -> None:
    """A midpoint score with no flag would read as a real judgement."""
    result = colour_harmony(outfit())
    assert result.informative is False
    assert "no colours" in result.detail


# -------------------------------------------------- formality coherence


def test_identical_formality_is_flagged_as_no_signal() -> None:
    """THE MOCK CASE, and the reason this flag exists.

    Every garment carrying formality 3 means spread is structurally zero, so
    this sub-score cannot separate any two outfits. It must not look like a
    confident 1.0.
    """
    result = formality_coherence(outfit(formality=3), target=3)
    assert result.informative is False
    assert "no spread signal" in result.detail


def test_real_spread_is_informative() -> None:
    items = [
        g("upper_base", "shirt_oxford", formality=3),
        g("lower", "chinos", formality=3),
        g("feet", "sneakers", formality=2),
    ]
    result = formality_coherence(items, target=3)
    assert result.informative is True


def test_a_wide_formality_spread_scores_lower_than_a_tight_one() -> None:
    tight = formality_coherence(
        [g("a", "x", formality=3), g("b", "y", formality=3), g("c", "z", formality=4)], target=3
    )
    wide = formality_coherence(
        [g("a", "x", formality=1), g("b", "y", formality=3), g("c", "z", formality=5)], target=3
    )
    assert tight.value > wide.value


def test_missing_the_occasion_target_costs_score() -> None:
    """Coherent but wrong is still wrong: joggers to an interview."""
    on_target = formality_coherence(
        [g("a", "x", formality=4), g("b", "y", formality=4), g("c", "z", formality=3)], target=4
    )
    off_target = formality_coherence(
        [g("a", "x", formality=1), g("b", "y", formality=1), g("c", "z", formality=2)], target=4
    )
    assert on_target.value > off_target.value


# ------------------------------------------------------------ weather fit


def test_the_warmest_item_drives_warmth_not_the_mean() -> None:
    """A coat over a t-shirt is a warm outfit.

    A mean would let the t-shirt cancel the coat and recommend it in summer.
    """
    coat_outfit = [
        g("upper_base", "t_shirt", warmth=1),
        g("lower", "jeans", warmth=2),
        g("upper_layer", "puffer_jacket", warmth=5),
        g("feet", "boots_ankle", warmth=4),
    ]
    assert weather_fit(coat_outfit, warmth_target=5).value == 1.0
    assert weather_fit(coat_outfit, warmth_target=1).value < 0.5


def test_identical_warmth_is_flagged_as_no_signal() -> None:
    result = weather_fit(outfit(warmth=2), warmth_target=2)
    assert result.informative is False


# ---------------------------------------------------------------- novelty


def test_never_worn_is_maximally_novel() -> None:
    result = novelty(outfit(), today=TODAY)
    assert result.value == 1.0
    assert result.informative is False  # nothing has ever been worn


def test_worn_yesterday_scores_lower_than_worn_last_month() -> None:
    recent = novelty([g("a", "x", wear_count=5, last_worn=TODAY - timedelta(days=1))], today=TODAY)
    stale = novelty([g("a", "x", wear_count=5, last_worn=TODAY - timedelta(days=40))], today=TODAY)
    assert recent.value < stale.value


def test_wear_count_is_log_damped_so_one_favourite_cannot_dominate() -> None:
    """Linear damping would make a 60-wear shirt score ~60x worse than a
    1-wear shirt, and wear counts follow a power law."""
    few = novelty([g("a", "x", wear_count=2, last_worn=TODAY)], today=TODAY).value
    many = novelty([g("a", "x", wear_count=60, last_worn=TODAY)], today=TODAY).value
    assert few > many
    # Log damping keeps them within the same order of magnitude.
    assert many > few * 0.4


# --------------------------------- style affinity and trends, once dead


def test_no_style_vector_and_no_trends_contribute_zero_and_say_why() -> None:
    """Weighted 0.20 and 0.05, reported rather than omitted when absent.

    Omitting them would mean the other four weights secretly sum to 0.75 and
    every score would be depressed for a reason no reader could see.

    THIS TEST USED TO ASSERT THE BUG. It checked for "Phase 8" and "Phase 11"
    in the detail strings — that is, it asserted the terms were UNWIRED, and
    it passed for three phases while 25% of the scoring weight was dead.
    A test that pins a stub in place is worse than no test, because it makes
    fixing the stub look like a regression.
    """
    s = style_affinity(outfit())
    tr = trend_alignment(outfit())
    assert s.value == 0.0 and "no style vector" in s.detail
    assert tr.value == 0.0 and "no published trend" in tr.detail
    # And uninformative, so `score_breakdown` says "contributed nothing"
    # rather than leaving a reader to wonder whether it is broken.
    assert not s.informative and not tr.informative


def test_style_affinity_rewards_alignment_and_clamps_dislike() -> None:
    """Cosine clamped at zero rather than rescaled to [0,1].

    `(cos+1)/2` would hand every outfit 0.5 — a tenth of the total score — for
    being merely ORTHOGONAL to the user's taste, and would let an outfit they
    demonstrably dislike still outscore nothing.
    """
    import numpy as np

    from stylist_domain.scoring import ScoredGarment

    dim = 8
    taste = np.zeros(dim)
    taste[0] = 1.0

    def built_from(vec) -> list[ScoredGarment]:
        return [
            ScoredGarment(
                garment_id=f"g{i}", slot="upper_base", subcategory="kurta", embedding=tuple(vec)
            )
            for i in range(2)
        ]

    aligned = style_affinity(built_from(taste), taste, events_applied=25)
    orthogonal = style_affinity(built_from(np.eye(dim)[3]), taste, events_applied=25)
    opposed = style_affinity(built_from(-taste), taste, events_applied=25)

    assert aligned.value == pytest.approx(1.0)
    assert orthogonal.value == pytest.approx(0.0)
    assert opposed.value == 0.0, "a disliked outfit must not outscore nothing"
    assert "clamped" in opposed.detail, "the clamp must be visible in the breakdown"


def test_a_thin_style_vector_is_not_trusted() -> None:
    """A vector built from three reactions is whichever outfit was rated first.

    Scoring against it would be self-reinforcing: the outfits it favours are
    the ones shown next, which are the ones rated next.
    """
    import numpy as np

    from stylist_domain.scoring import MIN_EVENTS_FOR_STYLE_AFFINITY, ScoredGarment

    taste = np.zeros(8)
    taste[0] = 1.0
    garments = [
        ScoredGarment(
            garment_id="g", slot="upper_base", subcategory="kurta", embedding=tuple(taste)
        )
    ]
    thin = style_affinity(garments, taste, events_applied=MIN_EVENTS_FOR_STYLE_AFFINITY - 1)
    enough = style_affinity(garments, taste, events_applied=MIN_EVENTS_FOR_STYLE_AFFINITY)

    assert thin.value == 0.0 and not thin.informative
    assert enough.value > 0.0 and enough.informative


def test_trend_alignment_averages_and_takes_the_strongest_field() -> None:
    """Averaged over garments so a five-piece outfit does not outscore a
    three-piece one for being larger, and the STRONGEST matching field per
    garment rather than the sum — a garment that is both a trending colour and
    a trending material is still one garment."""
    from stylist_domain.scoring import ScoredGarment

    trends = {
        ("subcategory", "kurta"): 1.0,
        ("primary_colour", "maroon"): 0.5,
        ("material", "silk"): 0.8,
    }
    both = ScoredGarment(
        garment_id="a",
        slot="upper_base",
        subcategory="kurta",
        primary_colour="maroon",
        material="silk",
    )
    # Strongest match is subcategory=kurta at 1.0, not 1.0+0.5+0.8.
    assert trend_alignment([both], trends).value == pytest.approx(1.0)

    untrending = ScoredGarment(garment_id="b", slot="lower", subcategory="jeans")
    # One garment at 1.0 and one at 0.0 averages to 0.5.
    assert trend_alignment([both, untrending], trends).value == pytest.approx(0.5)


def test_an_unpublished_value_scores_zero_rather_than_being_guessed() -> None:
    """Only trends that passed the k-anonymity floor are in `trends` at all.
    A value that is not there has no trend to be aligned with, which is
    different from being unfashionable."""
    from stylist_domain.scoring import ScoredGarment

    g = ScoredGarment(garment_id="a", slot="drape", subcategory="saree")
    result = trend_alignment([g], {("subcategory", "kurta"): 1.0})
    assert result.value == 0.0
    assert not result.informative, "no match is not the same as a zero trend"


# --------------------------------------------------------- hard penalty


def test_bold_pattern_penalty_reads_the_taxonomy_set() -> None:
    bold = list(load_taxonomy().bold_patterns)[:3]
    items = [
        g("upper_base", "kurta", pattern=bold[0]),
        g("lower", "palazzo", pattern=bold[1]),
        g("drape", "dupatta", pattern=bold[2]),
    ]
    multiplier, applied = hard_penalty(items)
    assert multiplier < 1.0
    assert any("bold_patterns" in a for a in applied), applied


def test_formality_spread_penalty_triggers_past_two() -> None:
    items = [g("a", "x", formality=1), g("b", "y", formality=4)]
    multiplier, applied = hard_penalty(items)
    assert any("formality_spread" in a for a in applied), applied
    assert multiplier < 1.0


def test_penalty_can_never_make_a_score_negative() -> None:
    """A negative score would sort above nothing and corrupt ranking."""
    bold = list(load_taxonomy().bold_patterns)[:3]
    items = [
        g("upper_base", "kurta", pattern=bold[0], primary_colour="red", formality=1),
        g("lower", "palazzo", pattern=bold[1], primary_colour="emerald", formality=5),
        g("drape", "dupatta", pattern=bold[2], primary_colour="mustard", formality=3),
        g("feet", "juttis", primary_colour="teal", formality=2),
    ]
    multiplier, _ = hard_penalty(items)
    assert multiplier >= 0.0


# ------------------------------------------------------------- the total


def test_a_rule_breaking_outfit_scores_exactly_zero() -> None:
    """The plan says forbidden combinations are "never surfaced".

    A merely low score still surfaces when the wardrobe is small.
    """
    result = score_outfit(
        [g("upper_base", "shirt_casual", primary_colour="white")],
        warmth_target=2,
        formality_target=2,
        today=TODAY,
    )
    assert result.total == 0.0
    assert result.valid is False
    assert result.violations


def test_breakdown_reports_how_much_of_the_score_was_informative() -> None:
    """The headline caveat, computed rather than remembered."""
    mock_like = score_outfit(
        outfit(formality=3, warmth=2),
        warmth_target=2,
        formality_target=3,
        today=TODAY,
    )
    assert mock_like.breakdown["informative_weight"] < 0.5, mock_like.breakdown


def test_every_sub_score_stays_within_bounds_under_random_input() -> None:
    """400 random outfits. Any sub-score outside 0..1 raises inside score_outfit,
    so this also asserts the guard itself fires rather than being decorative."""
    taxonomy = load_taxonomy()
    by_slot = {s: list(v) for s, v in taxonomy.subcategories_by_slot.items()}
    colours = list(taxonomy.colours)
    patterns = list(taxonomy.patterns)
    rng = random.Random(6123)

    for _ in range(400):
        items = []
        for slot in ("upper_base", "lower", "feet"):
            items.append(
                g(
                    slot,
                    rng.choice(by_slot[slot]),
                    primary_colour=rng.choice([*colours, None]),
                    pattern=rng.choice([*patterns, None]),
                    formality=rng.choice([None, 1, 2, 3, 4, 5]),
                    warmth=rng.choice([None, 1, 2, 3, 4, 5]),
                    wear_count=rng.randint(0, 80),
                    last_worn=rng.choice([None, TODAY - timedelta(days=rng.randint(0, 200))]),
                )
            )
        result = score_outfit(
            items,
            warmth_target=rng.randint(1, 5),
            formality_target=rng.randint(1, 5),
            today=TODAY,
        )
        assert 0.0 <= result.total <= 1.0, result.breakdown
        for name, sub in result.breakdown["sub_scores"].items():
            assert 0.0 <= sub["value"] <= 1.0, (name, sub)


def test_scoring_is_deterministic() -> None:
    """Same input, same score — the whole phase is "zero model calls"."""
    items = outfit(formality=3, warmth=3, primary_colour="blue_navy")
    a = score_outfit(items, warmth_target=3, formality_target=3, today=TODAY)
    b = score_outfit(items, warmth_target=3, formality_target=3, today=TODAY)
    assert a.total == b.total
    assert a.breakdown == b.breakdown
