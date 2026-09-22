"""Phase 6.1/6.2 — context resolution and the outfit slot-rule evaluator.

The property test is the point of this file. The plan asks for it in those
words — "generate random garment sets, assert every accepted outfit satisfies
every rule and every rejected one violates at least one" — and the reason is
that the rule table has seven interacting clauses over nine slots. Hand-picked
cases cover the combinations someone thought of.

Crucially the verification below is an INDEPENDENT re-implementation reading
the same taxonomy. Re-calling `evaluate()` to check `evaluate()` would pass
against any bug it contains.
"""

from __future__ import annotations

import random
from collections import Counter

import pytest

from stylist_domain.context import (
    PRECIP_PROBABILITY_WET,
    WIND_LAYER_KMH,
    resolve_context,
    warmth_for_feels_like,
)
from stylist_domain.slots import OutfitItem, evaluate
from stylist_domain.taxonomy import load_taxonomy


def item(slot: str, subcategory: str, n: int = 0) -> OutfitItem:
    return OutfitItem(f"{slot}:{subcategory}:{n}", slot, subcategory)


# --------------------------------------------------------------- context


def test_warmth_target_is_monotonic_in_temperature() -> None:
    """Hotter must never ask for more clothing.

    The table is expressed as ceilings (`feels_like_c_max`), which is easy to
    read backwards — inverting it would recommend an overcoat in May and pass
    every single-value test.
    """
    temps = list(range(45, -10, -1))
    warmths = [warmth_for_feels_like(t) for t in temps]
    assert warmths == sorted(warmths), list(zip(temps, warmths, strict=True))


def test_warmth_boundaries_match_the_taxonomy() -> None:
    """At a level's exact ceiling, that level is still correct.

    `feels_like_c_max` means "wear this up to here", inclusive. An off-by-one
    here silently shifts every recommendation by one step.
    """
    for level in load_taxonomy().raw["warmth"]:
        ceiling = float(level["feels_like_c_max"])
        assert warmth_for_feels_like(ceiling) >= int(level["value"]), level


def test_extreme_heat_clamps_rather_than_failing() -> None:
    """Above the lightest level's ceiling there is nothing lighter to pick."""
    assert warmth_for_feels_like(55.0) == 1


def test_every_occasion_resolves() -> None:
    """All 18 occasions must produce a context.

    The UI offers the taxonomy's list, so any occasion it cannot resolve is a
    selectable option that 500s.
    """
    for occasion in load_taxonomy().occasions:
        ctx = resolve_context(occasion=occasion, feels_like_c=24.0)
        assert 1 <= ctx.formality_target <= 5
        assert ctx.dress_code_target in load_taxonomy().dress_codes


def test_unknown_occasion_is_rejected_loudly() -> None:
    with pytest.raises(ValueError, match="unknown occasion"):
        resolve_context(occasion="brunch_with_aliens", feels_like_c=24.0)


def test_wind_adds_one_layer_and_says_so() -> None:
    """Reported, not silent: score_breakdown has to explain a heavier outfit."""
    calm = resolve_context(occasion="casual_outing", feels_like_c=24.0, wind_kmh=0)
    windy = resolve_context(occasion="casual_outing", feels_like_c=24.0, wind_kmh=WIND_LAYER_KMH)
    assert windy.warmth_target == calm.warmth_target + 1
    assert windy.wind_adjusted is True
    assert calm.wind_adjusted is False


def test_wind_cannot_push_warmth_past_the_scale() -> None:
    ctx = resolve_context(occasion="casual_outing", feels_like_c=-5.0, wind_kmh=80)
    assert ctx.warmth_target == 5


def test_wet_is_a_threshold_not_a_boolean_guess() -> None:
    dry = resolve_context(
        occasion="casual_outing",
        feels_like_c=28.0,
        precip_probability=PRECIP_PROBABILITY_WET - 0.01,
    )
    wet = resolve_context(
        occasion="casual_outing", feels_like_c=28.0, precip_probability=PRECIP_PROBABILITY_WET
    )
    assert dry.wet is False
    assert wet.wet is True


# ----------------------------------------------------- explicit rule cases


def test_a_western_outfit_is_valid() -> None:
    assert evaluate(
        [item("upper_base", "shirt_casual"), item("lower", "chinos"), item("feet", "loafers")]
    ).valid


def test_a_one_piece_outfit_is_valid() -> None:
    assert evaluate([item("full_body", "dress_midi"), item("feet", "heels_block")]).valid


def test_saree_with_a_blouse_is_a_complete_outfit() -> None:
    """Phase 6's exit criteria name composite ethnic garments explicitly.

    This failed before `lower_equivalent` existed: a saree sits in `drape`, so
    saree + choli_blouse + sandals satisfied neither [upper_base, lower] nor
    [full_body] and the single most important outfit in this market was
    rejected as incomplete.
    """
    result = evaluate(
        [
            item("drape", "saree"),
            item("upper_base", "choli_blouse"),
            item("feet", "sandals_flat"),
        ]
    )
    assert result.valid, result.violations


def test_a_saree_alone_is_incomplete_and_says_why() -> None:
    result = evaluate([item("drape", "saree"), item("feet", "sandals_flat")])
    assert not result.valid
    assert any("upper_base" in v for v in result.violations), result.violations


def test_a_saree_over_a_lower_garment_is_rejected() -> None:
    """The lower-equivalent count ADDS, so saree + churidar is 2, not 1."""
    result = evaluate(
        [
            item("drape", "saree"),
            item("lower", "churidar"),
            item("upper_base", "choli_blouse"),
            item("feet", "sandals_flat"),
        ]
    )
    assert not result.valid, result.violations


def test_a_dupatta_does_not_substitute_for_a_lower_garment() -> None:
    """A dupatta covers nothing. Treating every `drape` as lower-equivalent
    would accept "dupatta + blouse + sandals" as a complete outfit."""
    assert not evaluate(
        [
            item("drape", "dupatta"),
            item("upper_base", "choli_blouse"),
            item("feet", "sandals_flat"),
        ]
    ).valid


def test_a_dupatta_over_a_kurta_set_is_valid() -> None:
    assert evaluate(
        [
            item("drape", "dupatta"),
            item("upper_base", "kurta"),
            item("lower", "churidar"),
            item("feet", "juttis"),
        ]
    ).valid


def test_two_base_structures_at_once_is_rejected() -> None:
    """A dress AND a shirt and trousers is two outfits, not a richer one."""
    assert not evaluate(
        [
            item("full_body", "dress_midi"),
            item("upper_base", "shirt_casual"),
            item("lower", "chinos"),
            item("feet", "loafers"),
        ]
    ).valid


def test_forbidden_pairs_are_rejected() -> None:
    rules = load_taxonomy().raw["outfit_rules"]
    a, b = rules["forbidden_pairs"][0]
    slots = load_taxonomy().subcategories_by_slot
    slot_of = {sub: slot for slot, subs in slots.items() for sub in subs}
    result = evaluate(
        [
            item(slot_of[a], a),
            item(slot_of[b], b),
            item("upper_base", "shirt_casual"),
            item("lower", "chinos"),
            item("feet", "loafers"),
        ]
    )
    assert not result.valid
    assert any(a in v and b in v for v in result.violations), result.violations


def test_accessory_cap_is_five_not_three() -> None:
    """DECISION 4 raised it from 3 to 5 so festive looks are not hard-rejected."""
    base = [item("upper_base", "kurta"), item("lower", "churidar"), item("feet", "juttis")]
    five = base + [item("accessory", "bangles", n) for n in range(5)]
    six = base + [item("accessory", "bangles", n) for n in range(6)]
    assert evaluate(five).valid
    assert not evaluate(six).valid


# --------------------------------------------------------- property test


def _independently_valid(items: list[OutfitItem]) -> bool:
    """A SECOND implementation of the rules, written from the taxonomy.

    Deliberately structured differently from `evaluate` — sets and explicit
    counts rather than a violation list — so a shared misreading is unlikely
    to survive in both.
    """
    rules = load_taxonomy().raw["outfit_rules"]
    counts = Counter(i.slot for i in items)
    subs = [i.subcategory for i in items]
    equiv = set(rules.get("lower_equivalent", []))

    lower_total = counts["lower"] + sum(1 for s in subs if s in equiv)

    def group_ok(group: list[str]) -> bool:
        return all((lower_total if s == "lower" else counts[s]) == 1 for s in group)

    if sum(1 for g in rules["exactly_one_of"] if group_ok(list(g))) != 1:
        return False
    if any(counts[s] != 1 for s in rules["exactly_one"]):
        return False
    if any(counts[s] > 1 for s in rules["at_most_one"]):
        return False
    # `prefer_one` is checked like `at_most_one`: never two, never required.
    # Whether the slot SHOULD have been filled is a question about the
    # wardrobe, and neither this reference nor `evaluate` can see one — they
    # both take a list of garments. The generator is what knows footwear was
    # available; this only forbids a second pair.
    if any(counts[s] > 1 for s in rules.get("prefer_one", [])):
        return False
    for spec in rules["ranges"]:
        if not (int(spec["min"]) <= counts[spec["slot"]] <= int(spec["max"])):
            return False
    for spec in rules["requires"]:
        if spec["subcategory"] in subs and counts[spec["needs_slot"]] < 1:
            return False
    return all(not (a in subs and b in subs) for a, b in rules["forbidden_pairs"])


def test_property_accepted_outfits_satisfy_every_rule() -> None:
    """1,500 random garment sets, cross-checked against an independent impl.

    Random sets are mostly invalid, which is the useful part: it exercises
    every rejection path, and any disagreement between the two implementations
    is a real defect in one of them.
    """
    taxonomy = load_taxonomy()
    by_slot = {s: list(v) for s, v in taxonomy.subcategories_by_slot.items()}
    rng = random.Random(20260911)
    slots = list(by_slot)

    agreed = 0
    valid_seen = 0
    for _ in range(1500):
        n = rng.randint(1, 7)
        items = []
        for k in range(n):
            slot = rng.choice(slots)
            items.append(item(slot, rng.choice(by_slot[slot]), k))
        mine = evaluate(items)
        theirs = _independently_valid(items)
        assert mine.valid == theirs, (
            f"disagreement: evaluate={mine.valid} independent={theirs}\n"
            f"items={[(i.slot, i.subcategory) for i in items]}\n"
            f"violations={mine.violations}"
        )
        # An accepted outfit must carry no violations, and a rejected one must
        # name at least one — an empty reason list is unactionable.
        if mine.valid:
            assert mine.violations == ()
            valid_seen += 1
        else:
            assert mine.violations
        agreed += 1

    assert agreed == 1500
    # If random generation never produced a valid outfit the test would pass
    # while proving nothing about the accept path.
    assert valid_seen > 0, "no valid outfit generated; the accept path is untested"
