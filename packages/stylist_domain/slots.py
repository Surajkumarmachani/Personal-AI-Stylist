"""Step 6.2 — the outfit slot-rule evaluator.

TABLE-DRIVEN, NOT HAND-CODED
----------------------------
Every rule is read from `outfit_rules` in taxonomy.yaml at call time. None of
them is expressed in Python. That matters because the rules encode product
decisions that will be argued about — DECISION 1 models a saree as `drape` +
a REQUIRED `upper_base`, DECISION 4 raised the accessory cap from 3 to 5 —
and those arguments should be settled by editing a reviewed data file, not by
finding the right `if` in a scorer.

The plan's phrasing is "replaces {top+bottom} or {one_piece}", and the reason
is exactly the ethnic-wear case: a saree with no blouse is not an outfit, a
kurta with churidar is, and a lehenga skirt needs a choli. A hand-written
`top and bottom or one_piece` cannot express any of that.

VIOLATIONS ARE RETURNED, NOT RAISED
-----------------------------------
The candidate generator assembles hundreds of combinations and most are
invalid; that is normal, not exceptional. Returning the reasons also gives the
UI something to say ("add a blouse") and the scorer something to attribute a
hard penalty to, instead of a silent zero.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from stylist_domain.taxonomy import load_taxonomy


@dataclass(frozen=True)
class OutfitItem:
    """The minimum an outfit rule needs to know about a garment."""

    garment_id: str
    slot: str
    subcategory: str


@dataclass(frozen=True)
class RuleResult:
    valid: bool
    violations: tuple[str, ...]

    def __bool__(self) -> bool:
        return self.valid


def evaluate(items: tuple[OutfitItem, ...] | list[OutfitItem]) -> RuleResult:
    """Check a candidate outfit against every hard rule.

    Soft penalties (saturated hues, bold patterns, formality spread) are NOT
    applied here — they belong to the scorer, which can weigh them. Mixing the
    two would make a merely unusual outfit unrepresentable rather than
    low-scoring.
    """
    rules = load_taxonomy().raw["outfit_rules"]
    slots = Counter(item.slot for item in items)
    subcats = {item.subcategory for item in items}
    violations: list[str] = []

    # ---- exactly_one_of: pick exactly one base structure --------------
    #
    # [[upper_base, lower], [full_body]] — a shirt with trousers, OR a dress.
    # Both at once is not a richer outfit, it is two outfits; neither is an
    # incomplete one. A group counts as satisfied only when EVERY slot in it
    # appears exactly once, so "one shirt, no trousers" fails rather than
    # half-passing.
    # A saree covers the lower body, so it satisfies `lower` without a separate
    # lower garment (taxonomy `lower_equivalent`). Counted as an ADDITION to
    # the real lower count, not a substitution, so a saree worn over churidar
    # gives an effective count of 2 and is correctly rejected.
    lower_equivalent = set(rules.get("lower_equivalent", []))
    effective = dict(slots)
    substitutes = sum(1 for item in items if item.subcategory in lower_equivalent)
    if substitutes:
        effective["lower"] = effective.get("lower", 0) + substitutes

    groups: list[list[str]] = [list(g) for g in rules.get("exactly_one_of", [])]
    satisfied = [g for g in groups if all(effective.get(slot, 0) == 1 for slot in g)]
    if len(satisfied) != 1:
        readable = " or ".join("+".join(g) for g in groups)
        if not satisfied:
            violations.append(f"needs exactly one of: {readable}")
        else:
            violations.append(f"satisfies more than one base structure ({readable}); pick one")

    # ---- exactly_one: required, single ---------------------------------
    for slot in rules.get("exactly_one", []):
        count = slots.get(slot, 0)
        if count != 1:
            violations.append(f"needs exactly one {slot} (has {count})")

    # ---- prefer_one: at most one, never required ------------------------
    #
    # Checked like `at_most_one` on purpose. Whether the slot SHOULD have been
    # filled is a question about the wardrobe, which `evaluate` cannot see —
    # it takes a list of garments, not a pool. The generator is what knows
    # there were shoes available and used them; this only enforces that an
    # outfit never carries two pairs.
    for slot in rules.get("prefer_one", []):
        count = slots.get(slot, 0)
        if count > 1:
            violations.append(f"at most one {slot} (has {count})")

    # ---- at_most_one: optional, never doubled --------------------------
    for slot in rules.get("at_most_one", []):
        count = slots.get(slot, 0)
        if count > 1:
            violations.append(f"at most one {slot} (has {count})")

    # ---- ranges ---------------------------------------------------------
    for spec in rules.get("ranges", []):
        slot = spec["slot"]
        count = slots.get(slot, 0)
        low, high = int(spec["min"]), int(spec["max"])
        if not (low <= count <= high):
            violations.append(f"{slot} must be {low}-{high} (has {count})")

    # ---- requires: composite garments ----------------------------------
    #
    # DECISION 1: a saree occupies `drape` and REQUIRES an `upper_base`. This
    # is the rule that makes "saree alone" an incomplete outfit rather than a
    # valid one-piece, and it is why the blouse stays a separate garment with
    # its own cost-per-wear.
    for spec in rules.get("requires", []):
        if spec["subcategory"] in subcats and slots.get(spec["needs_slot"], 0) < 1:
            hint = spec.get("hint")
            violations.append(
                f"{spec['subcategory']} requires a {spec['needs_slot']}"
                + (f" ({hint})" if hint else "")
            )

    # ---- forbidden pairs -------------------------------------------------
    for pair in rules.get("forbidden_pairs", []):
        a, b = pair[0], pair[1]
        if a in subcats and b in subcats:
            violations.append(f"{a} + {b} is not a combination we surface")

    return RuleResult(valid=not violations, violations=tuple(violations))


def required_slots() -> tuple[str, ...]:
    """Slots an outfit genuinely cannot be assembled without.

    EMPTY NOW, and that is the point. `feet` used to be here, which meant a
    wardrobe with no catalogued footwear produced zero outfits — see
    `preferred_slots`. The function stays because `exactly_one_of` (the base
    structure) is still mandatory and a future rule may want this tier again;
    an empty tuple is a real answer, not a stub.
    """
    rules = load_taxonomy().raw["outfit_rules"]
    return tuple(rules.get("exactly_one", []))


def preferred_slots() -> tuple[str, ...]:
    """Slots to fill WHENEVER the wardrobe can, without which an outfit is
    still valid.

    The middle tier between required and optional, and both neighbours are
    wrong for footwear:

      required   a wardrobe with no shoes produced NOTHING. Correct about
                 dressing, wrong about software — someone who has catalogued
                 six shirts and no shoes has told us plenty.
      optional   shoes become merely allowed, so a shoeless outfit can
                 outrank a shod one for someone who owns shoes. The generator
                 would sometimes not bother.

    Preferred means: include one if there is one, never two, and never fail
    for want of it.
    """
    rules = load_taxonomy().raw["outfit_rules"]
    return tuple(rules.get("prefer_one", []))


def base_structures() -> tuple[tuple[str, ...], ...]:
    """The alternative base slot-sets, e.g. ((upper_base, lower), (full_body,))."""
    rules = load_taxonomy().raw["outfit_rules"]
    return tuple(tuple(g) for g in rules.get("exactly_one_of", []))


def optional_slots() -> tuple[str, ...]:
    """Slots that may appear, with their caps applied by `evaluate`."""
    rules = load_taxonomy().raw["outfit_rules"]
    at_most = list(rules.get("at_most_one", []))
    ranged = [str(spec["slot"]) for spec in rules.get("ranges", [])]
    return tuple(dict.fromkeys(at_most + ranged))
