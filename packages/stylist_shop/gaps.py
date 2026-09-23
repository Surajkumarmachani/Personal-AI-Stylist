"""Which slot the wardrobe cannot fill, and what would fix it.

WHY THIS IS NOT "RECOMMENDED PRODUCTS"
--------------------------------------
A feed of things to buy is what every retailer already shows, and putting one
beside "every look is built from clothes you own" undermines the only claim
this app has that they do not.

A GAP is different and specific: the candidate pool for this occasion came
back with no `feet`, or nothing in `formal_ethnic`, or nothing warm enough for
8°C — and the system knows which, because the same filters that produced the
outfits produced the emptiness. One item would turn "I can't dress you for
this" into an outfit.

That is a recommendation the user asked for by trying to get dressed, not one
the app volunteered.

RANKED BY WHAT THE WARDROBE ALREADY CONTAINS
--------------------------------------------
A product is better when it goes with more of what you own. `colour_harmony`
already measures that between garments, so the same measure ranks candidates:
a pair of shoes that works with six of your outfits beats one that works with
one, and neither is chosen for being expensive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from stylist_domain.slots import base_structures, preferred_slots, required_slots


@dataclass(frozen=True, slots=True)
class WardrobeGap:
    """A slot that stopped this occasion being dressed."""

    slot: str
    occasion: str
    # What the pool was looking for, so a product can be matched on the same
    # terms rather than on the slot alone. A `feet` gap for an interview is
    # not filled by trainers.
    dress_code: str | None
    warmth_target: int | None
    # `blocking` when no outfit exists at all; `incomplete` when outfits are
    # shown but missing this piece. Different sentences, different urgency.
    severity: str
    reason: str


def find_gaps(pool: Any, ctx: Any) -> list[WardrobeGap]:
    """Read the gaps out of a candidate pool that has already been built.

    Derived from the SAME pool the suggestions were made from, never from a
    second query. A gap computed independently would eventually disagree with
    the outfits on screen — telling someone they have no footwear on a page
    showing three outfits with shoes is the kind of contradiction that makes
    the whole screen untrustworthy.
    """
    gaps: list[WardrobeGap] = []

    def add(slot: str, severity: str, reason: str) -> None:
        gaps.append(
            WardrobeGap(
                slot=slot,
                occasion=getattr(ctx, "occasion", ""),
                dress_code=getattr(ctx, "dress_code_target", None),
                warmth_target=getattr(ctx, "warmth_target", None),
                severity=severity,
                reason=reason,
            )
        )

    # Blocking: without these there is no outfit at all.
    for slot in required_slots():
        if not pool.by_slot.get(slot):
            add(slot, "blocking", f"no {slot.replace('_', ' ')} that suits this occasion")

    # BASE STRUCTURE: report the CHEAPEST ROUTE TO ONE OUTFIT, not every route.
    #
    # `exactly_one_of` is [[upper_base, lower], [full_body]] — a shirt with
    # trousers OR a dress. They are alternative solutions to the same problem,
    # so listing both as blocking tells someone with neither that they are
    # missing three things when buying two would fix it, and one of those
    # three (`full_body`) may have nothing in the catalogue at all.
    #
    # Only reported when NO structure is already satisfiable: a wardrobe of
    # dresses is not missing trousers.
    if not any(all(pool.by_slot.get(s) for s in group) for group in base_structures()):
        # EVERY route, and the caller drops the ones it cannot fill.
        #
        # Picking the route with the fewest missing slots looked cleaner and
        # was worse: `[full_body]` needs one item where `[upper_base, lower]`
        # needs two, so it always won — and the catalogue had no full_body
        # stock, leaving a blocking gap with nothing to offer and the useful
        # answer suppressed.
        #
        # Which route is actionable depends on what a merchant has, which this
        # module deliberately cannot see. So it states the facts and the
        # caller, which does see the catalogue, decides.
        for group in base_structures():
            for slot in group:
                if not pool.by_slot.get(slot):
                    add(slot, "blocking", f"no {slot.replace('_', ' ')} to build a base outfit")

    # Incomplete: outfits exist, but they are missing a piece.
    for slot in preferred_slots():
        if not pool.by_slot.get(slot):
            add(slot, "incomplete", f"outfits are being shown without {slot.replace('_', ' ')}")

    # Deduplicated on slot, blocking first — one line per slot is what a user
    # can act on, and the same slot reported twice reads as a bug.
    seen: dict[str, WardrobeGap] = {}
    for gap in sorted(gaps, key=lambda g: 0 if g.severity == "blocking" else 1):
        seen.setdefault(gap.slot, gap)
    return list(seen.values())
