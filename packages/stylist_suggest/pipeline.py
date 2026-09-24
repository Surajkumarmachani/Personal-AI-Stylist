"""Step 6.2 — candidate generation and assembly. Zero model calls.

THE SHAPE, AND WHY IT IS THIS SHAPE
-----------------------------------
    1  hard SQL filters   -> a wearable pool, done in the database
    2  anchor selection   -> 8-12 items, mostly affinity, some exploration
    3  complements        -> per anchor, per slot, nearest by embedding
    4  assemble + score   -> against the slot rules, then the scorer
    -> 200-500 candidates

The filters run in SQL rather than in Python because a 400-item wardrobe with
laundry, warmth, dress-code and climate constraints usually reduces to a few
dozen candidates, and shipping 400 rows plus 400 768-dimension vectors over
the wire to discard 90% of them is the difference between the plan's <100ms
budget and not meeting it.

WHY ANCHORS AT ALL
------------------
Every combination of a 400-item wardrobe is astronomically large, and almost
all of it is rubbish. Anchoring on 8-12 plausible items and completing each one
turns an intractable search into a bounded one, at the cost of missing outfits
whose merit only appears as a whole. That trade is why the count is 8-12 rather
than 3: too few anchors and the day's suggestions all look like variations on
one shirt.

EXPLORATION IS DELIBERATE, NOT NOISE
------------------------------------
Some anchor slots go to LOW-WEAR items on purpose. A recommender that only
ranks by affinity converges on the six things you already wear, which is
precisely the problem this product exists to solve — the clothes worth
surfacing are the ones you forgot you own.
"""

from __future__ import annotations

import hashlib
import itertools
import random
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import text

from stylist_domain.context import OutfitContext
from stylist_domain.scoring import (
    OutfitScore,
    ScoredGarment,
    load_scoring_config,
    score_outfit,
)
from stylist_domain.slots import (
    base_structures,
    evaluate,
    optional_slots,
    preferred_slots,
    required_slots,
)
from stylist_domain.style import parse_embedding
from stylist_domain.taxonomy import load_taxonomy

# 8-12 per the plan. The upper bound is a latency budget, not a taste
# judgement: each anchor costs one pgvector query per slot.
ANCHOR_COUNT = 10
# Of those, how many are reserved for low-wear exploration.
EXPLORATION_ANCHORS = 3
# Complements retrieved per (anchor, slot). Small because they are already
# filtered and ordered by similarity; the assembly step multiplies them.
COMPLEMENTS_PER_SLOT = 4
# The plan's target band. Generating far more than this wastes scoring time on
# combinations that will never be surfaced.
MAX_CANDIDATES = 500
# A garment worn within this window is skipped — you do not want yesterday's
# outfit suggested again this morning.
RECENTLY_WORN_DAYS = 3


@dataclass
class CandidatePool:
    """The wearable wardrobe for one context, grouped by slot."""

    by_slot: dict[str, list[ScoredGarment]] = field(default_factory=dict)
    # Reasons the pool is thin, surfaced to the caller rather than logged.
    # "No suggestions" with no explanation is the least actionable failure a
    # wardrobe app can produce.
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.by_slot.values())


@dataclass
class SuggestionResult:
    outfits: list[tuple[tuple[ScoredGarment, ...], OutfitScore]]
    candidates_considered: int
    pool: CandidatePool


def garment_set_hash(garment_ids: list[str] | tuple[str, ...]) -> str:
    """Identity of an outfit as a SET, not a list.

    Sorted before hashing so the same three garments in any order collide —
    which is what makes the unique index on (user, hash, context) correct.
    """
    joined = "|".join(sorted(str(g) for g in garment_ids))
    return hashlib.sha256(joined.encode()).hexdigest()


# How many warmth levels either side of the target still count as wearable.
#
# One, normally: the scorer is what should prefer an exact match, and a hard
# filter demanding equality would empty the wardrobe on most days.
#
# But it IS a hard filter, and on a required slot a hard filter with one
# garment behind it is a single point of failure. Measured on this wardrobe:
# the owner's only footwear is canvas sneakers tagged `warmth=1`, the default
# 26C placeholder resolves to `warmth_target=3`, and |1-3| = 2 — so the one
# pair of shoes was filtered out and EVERY occasion answered "no wearable
# feet". `_rescue_required_slots` is the fix, not a wider tolerance here:
# widening this would also let a puffer jacket through at 30C.
WARMTH_TOLERANCE = 1

# The rescue pass's tolerance: effectively "any warmth at all". The warmth
# ladder has five levels, so anything >= 4 disables the predicate; 99 says
# that outright rather than encoding the ladder's length in a second place.
WARMTH_RELAXED = 99

POOL_SQL = """
            SELECT g.id, g.slot::text AS slot, g.subcategory::text AS subcategory,
                   g.primary_colour::text AS primary_colour,
                   g.secondary_colour::text AS secondary_colour,
                   g.pattern::text AS pattern, g.material::text AS material,
                   g.formality, g.warmth, g.climate_bands,
                   COALESCE(w.wear_count, 0) AS wear_count,
                   w.last_worn,
                   -- Needed only by `style_affinity`. Cast to text because
                   -- pgvector's adapter is registered for the ORM mapping and
                   -- not for text() queries; `parse_embedding` handles it.
                   g.embedding::text AS embedding
            FROM garments g
            LEFT JOIN (
                SELECT garment_id, count(*) AS wear_count, max(worn_on) AS last_worn
                FROM wear_log GROUP BY garment_id
            ) w ON w.garment_id = g.id
            WHERE g.is_active
              AND (CAST(:slot AS text) IS NULL OR g.slot::text = CAST(:slot AS text))
              AND g.state NOT IN ('rejected', 'quarantined', 'duplicate_suspect')
              -- in the wash is a hard exclusion: it is not in the wardrobe today
              AND NOT g.needs_wash
              -- unset warmth is KEPT. A garment whose tagging degraded is still
              -- wearable, and excluding it would make a degraded ingest look
              -- like a lost garment.
              -- :warmth_slack is 1 normally. `_rescue_required_slots` re-runs
              -- this query with it wide open for a REQUIRED slot that nothing
              -- satisfied, because an empty `feet` is not a ranking problem,
              -- it is no outfit at all.
              AND (g.warmth IS NULL OR abs(g.warmth - :warmth_target) <= :warmth_slack)
              AND (g.dress_code IS NULL OR g.dress_code::text = ANY(:compatible))
              -- climate_bands empty means "not yet classified", not "unsuitable"
              AND (
                    g.climate_bands IS NULL
                 OR cardinality(g.climate_bands) = 0
                 OR :climate_any
              )
              AND (
                    cardinality(CAST(:poor_wet AS text[])) = 0
                 OR g.material IS NULL
                 OR NOT (g.material::text = ANY(CAST(:poor_wet AS text[])))
              )
              AND (
                    w.last_worn IS NULL
                 OR w.last_worn < CURRENT_DATE - make_interval(days => :recent_days)
              )
              -- PREFERENCE FACTS, kind='never'. A hard exclusion, because that
              -- is what the word means: "never yellow" is not "yellow ranks
              -- lower". `avoids` is applied in Python below, where it can be
              -- relaxed rather than emptying a slot.
              --
              -- `preference_fact` is RLS-scoped like every other tenant table,
              -- so this needs no user_id predicate and cannot read anyone
              -- else's rules.
              AND NOT EXISTS (
                SELECT 1 FROM preference_fact pf
                WHERE pf.kind = 'never'
                  AND (
                       (pf.field_name = 'subcategory' AND pf.field_value = g.subcategory::text)
                    OR (pf.field_name = 'primary_colour'
                        AND pf.field_value = g.primary_colour::text)
                    OR (pf.field_name = 'material' AND pf.field_value = g.material::text)
                    OR (pf.field_name = 'fit'      AND pf.field_value = g.fit::text)
                    OR (pf.field_name = 'pattern'  AND pf.field_value = g.pattern::text)
                  )
              )
            """


def _to_garment(r: Any) -> ScoredGarment:
    """One candidate row. Shared by the main query and the rescue pass, so the
    two cannot drift in what they read."""
    return ScoredGarment(
        garment_id=str(r["id"]),
        slot=r["slot"],
        subcategory=r["subcategory"],
        primary_colour=r["primary_colour"],
        secondary_colour=r["secondary_colour"],
        pattern=r["pattern"],
        material=r["material"],
        formality=r["formality"],
        warmth=r["warmth"],
        wear_count=int(r["wear_count"]),
        last_worn=r["last_worn"],
        # pgvector comes back as a STRING through text() queries — the type
        # adapter is registered for the ORM mapping, not for raw SQL. Same
        # trap Phase 8 hit; `parse_embedding` is the shared answer.
        embedding=tuple(parse_embedding(r["embedding"]) or ()) or None,
    )


async def _rescue_required_slots(
    session: Any, pool: CandidatePool, ctx: OutfitContext, params: dict[str, Any]
) -> None:
    """Re-run one required slot with the warmth match relaxed, and SAY SO.

    RELAXING BEATS EMPTYING, and this codebase already argues the point:
    `_apply_avoids` drops a preference for a slot it would otherwise empty,
    because "I avoid crop tops" never meant "I would rather have no outfit".
    The same reasoning holds here with more force, since warmth is a MODEL's
    guess rather than a rule the user stated.

    What went wrong without it: the owner's only footwear is canvas sneakers
    the tagger scored `warmth=1` (the ladder's examples for level 1 are
    `tank_top` and `sandals_flat`, so 2 was nearer). The default weather
    placeholder is 26C, which resolves to `warmth_target=3`. |1 - 3| = 2, one
    step outside the tolerance, so the shoes vanished and every occasion
    answered "no wearable feet — every outfit needs one". One garment, one
    level of tagger error, and the product returns nothing at all.

    Only for REQUIRED slots, and only when the slot is otherwise EMPTY. An
    optional slot with no candidates is just an outfit without a jacket; a
    required slot with none is no outfit. The scorer still ranks a relaxed
    garment below a true warmth match, so this changes what is POSSIBLE
    without changing what is PREFERRED.
    """
    # REQUIRED AND PREFERRED BOTH. `feet` moved out of required when footwear
    # stopped being mandatory, and dropping it from the rescue at the same
    # time would have quietly undone the fix this function exists for: the
    # owner's only shoes are `warmth=1` against a target of 3, so without the
    # rescue they are filtered out and every outfit comes back shoeless — for
    # someone who owns shoes. Optional slots are excluded: an outfit without a
    # bag is not missing anything.
    for slot in (*required_slots(), *preferred_slots()):
        if pool.by_slot.get(slot):
            continue
        rows = await session.execute(
            text(POOL_SQL), dict(params, slot=slot, warmth_slack=WARMTH_RELAXED)
        )
        found = [_to_garment(r) for r in rows.mappings()]
        if not found:
            # Something OTHER than warmth emptied this slot. Leave it empty and
            # let the caller's note stand — inventing a reason here would be
            # the same mistake as a note that blames the laundry basket for a
            # warmth filter.
            continue
        pool.by_slot[slot] = found
        levels = sorted({g.warmth for g in found if g.warmth is not None})
        shown = ", ".join(str(v) for v in levels) or "unset"
        pool.notes.append(
            f"relaxed the warmth match for {slot}: the only options are warmth "
            f"{shown} against a target of {ctx.warmth_target} — they rank lower, "
            f"but an outfit needs {slot}"
        )


async def load_wardrobe(session: Any, ctx: OutfitContext) -> CandidatePool:
    """Hard filters, in SQL. Everything here is a hard requirement.

    Soft preferences belong to the scorer: a garment one warmth step from
    target should rank lower, not vanish, which is why the warmth filter has a
    tolerance rather than an equality.
    """
    taxonomy = load_taxonomy()
    # dress_codes_compatible(a, b) is a PREDICATE, not a lookup — the map is
    # what we need here, and it is asymmetric on purpose: `casual` accepts
    # smart_casual, but a `business` occasion does not accept casual.
    compatible = list(
        taxonomy.dress_code_compatibility.get(ctx.dress_code_target, [ctx.dress_code_target])
    )
    if ctx.dress_code_target not in compatible:
        compatible.append(ctx.dress_code_target)

    # Rain is a material constraint, not a preference. A silk saree in a
    # downpour is not a low-scoring suggestion, it is a bad one.
    monsoon = taxonomy.raw.get("monsoon_suitability", {})
    poor_when_wet = list(monsoon.get("poor", [])) if ctx.wet else []

    params: dict[str, Any] = {
        "warmth_target": ctx.warmth_target,
        "warmth_slack": WARMTH_TOLERANCE,
        "slot": None,
        "compatible": compatible,
        # Climate matching needs the band set for this warmth level, which
        # the taxonomy derives rather than storing per garment.
        "climate_any": True,
        "poor_wet": poor_when_wet,
        "recent_days": RECENTLY_WORN_DAYS,
    }
    rows = await session.execute(text(POOL_SQL), params)

    pool = CandidatePool()
    for r in rows.mappings():
        item = _to_garment(r)
        pool.by_slot.setdefault(item.slot, []).append(item)

    await _apply_avoids(session, pool)
    await _rescue_required_slots(session, pool, ctx, params)

    # Fail fast and SAY WHY, for slots an outfit genuinely cannot do without.
    for slot in required_slots():
        if not pool.by_slot.get(slot):
            pool.notes.append(
                f"no wearable {_SLOT_WORDS.get(slot, slot.replace('_', ' '))} — every "
                f"outfit needs one (check the laundry basket, and whether anything "
                f"you own suits {str(ctx.dress_code_target).replace('_', ' ')})"
            )

    # A PREFERRED SLOT IS A NOTE, NOT A FAILURE. The outfits below are real
    # and wearable; they are just missing a piece the wardrobe cannot supply.
    # Said plainly rather than silently, because "why do none of these have
    # shoes?" is the obvious next question and the answer is actionable.
    for slot in preferred_slots():
        if not pool.by_slot.get(slot):
            # "no wearable feet" is what the slot id produced, and it is not a
            # sentence about clothes. The slot is named for the body part; the
            # user owns garments.
            pool.notes.append(
                "no shoes that work for this — these outfits are shown without "
                "any. Add a pair and I'll include it."
                if slot == "feet"
                else (
                    f"no wearable {_SLOT_WORDS.get(slot, slot.replace('_', ' '))}; "
                    f"outfits are shown without one"
                )
            )
    if not any(all(pool.by_slot.get(s) for s in structure) for structure in base_structures()):
        pool.notes.append(_base_structure_note(pool, ctx))
    return pool


# How to say a slot to someone who has never read the taxonomy. The ids are
# machine words: a reply that ends "(upper_base+lower or full_body)" tells a
# user nothing they can act on, and it reached them — that string was the
# whole of the answer to "Cultural wear".
_SLOT_WORDS = {
    "upper_base": "a top",
    "lower": "something to wear on the bottom",
    "full_body": "a one-piece like a kurta set, dress or saree",
    "feet": "shoes",
    "upper_outer": "a jacket or layer",
}


def _base_structure_note(pool: CandidatePool, ctx: Any) -> str:
    """Say what is actually missing, in words, and what would fix it.

    THE SLOT IS KNOWN, SO NAME IT. The old note listed every base structure
    the taxonomy defines and left the user to work out which half they were
    short of. The pool knows precisely: this reports the structure that is
    CLOSEST to complete, because that is the smallest thing the user could
    add to get an outfit.

    The distinction that matters is between owning nothing for the dress code
    and owning part of it — "you have no ethnic clothes" and "you have an
    ethnic top but no bottom" call for different next steps, and the second is
    the far more common and more frustrating one.
    """
    code = str(getattr(ctx, "dress_code_target", "") or "").replace("_", " ")
    base_slots = {s for structure in base_structures() for s in structure}
    has_any = any(pool.by_slot.get(s) for s in base_slots)

    if not has_any:
        return (
            f"there's nothing {code} in your wardrobe yet — add a few pieces "
            f"and I'll style them"
        )

    # Fewest additions first: with an ethnic top already owned, "add a bottom"
    # beats "add a one-piece", even though both would work.
    missing = min(
        (tuple(s for s in structure if not pool.by_slot.get(s)) for structure in base_structures()),
        key=len,
    )
    words = " and ".join(_SLOT_WORDS.get(s, s.replace("_", " ")) for s in missing)
    return (
        f"your {code} pieces don't make a full outfit yet — add {words} "
        f"and I'll put it together"
    )


# The garment attributes a preference fact can name. Kept in sync with
# `FACT_FIELDS` in the feedback router, which refuses to store a fact about
# anything else — a fact the pipeline cannot apply is a promise to the user
# that nothing keeps.
FACT_ATTRS = ("subcategory", "primary_colour", "material", "fit", "pattern")


async def _apply_avoids(session: Any, pool: CandidatePool) -> None:
    """Apply `avoids` facts per slot, RELAXING rather than emptying a slot.

    `never` is enforced in SQL because it is absolute. `avoids` is softer by
    design — "I avoid crop tops" means "not usually", not "I would rather have
    no outfit" — so it is applied here, where the pool is visible and the rule
    can be dropped for a slot it would otherwise empty.

    That relaxation is the whole reason this is not another SQL predicate. A
    wardrobe of nine shirts, six of which the user avoids, should still produce
    an outfit; a hard filter would return an empty screen and the user would
    have no way to connect it to a preference they set weeks ago. When it
    happens the pool SAYS SO, so the UI can explain rather than just showing
    less.
    """
    rows = await session.execute(
        text("SELECT field_name, field_value FROM preference_fact WHERE kind = 'avoids'")
    )
    avoids = [(r["field_name"], r["field_value"]) for r in rows.mappings()]
    if not avoids:
        return

    def matches(item: ScoredGarment) -> bool:
        return any(
            field in FACT_ATTRS and getattr(item, field, None) == value for field, value in avoids
        )

    relaxed: list[str] = []
    applied = 0
    for slot, items in pool.by_slot.items():
        kept = [i for i in items if not matches(i)]
        if not kept and items:
            # Dropping this rule beats returning nothing. Recorded so the
            # caller can tell the user their preference was overridden — a
            # silently ignored rule is how a legible system stops being
            # trusted.
            relaxed.append(slot)
            continue
        applied += len(items) - len(kept)
        pool.by_slot[slot] = kept

    if applied:
        pool.notes.append(f"{applied} garment(s) hidden by your 'avoids' preferences")
    for slot in relaxed:
        pool.notes.append(
            f"your 'avoids' preference was relaxed for {slot} — "
            f"nothing else in your wardrobe fits today's outfit"
        )


def _pick_anchors(
    pool: CandidatePool, ctx: OutfitContext, rng: random.Random
) -> list[ScoredGarment]:
    """8-12 items to build around: mostly plausible, some deliberately not.

    "Plausible" is formality proximity to the occasion, because that is the
    signal available without the Phase 8 style vector. Exploration slots go to
    the LEAST worn items, which is the whole point of the product.
    """
    base_slots = {s for structure in base_structures() for s in structure}
    # Anchor on base garments only. Anchoring on a bag produces an outfit
    # assembled around a bag.
    pool_items = [g for slot, items in pool.by_slot.items() if slot in base_slots for g in items]
    if not pool_items:
        return []

    def affinity(g: ScoredGarment) -> float:
        if g.formality is None:
            return 0.5
        return 1.0 - abs(g.formality - ctx.formality_target) / 4.0

    affinity_ranked = sorted(pool_items, key=affinity, reverse=True)
    exploration_ranked = sorted(pool_items, key=lambda g: (g.wear_count, g.garment_id))

    chosen: list[ScoredGarment] = []
    seen: set[str] = set()
    for g in affinity_ranked[: ANCHOR_COUNT - EXPLORATION_ANCHORS]:
        if g.garment_id not in seen:
            chosen.append(g)
            seen.add(g.garment_id)
    for g in exploration_ranked:
        if len(chosen) >= ANCHOR_COUNT:
            break
        if g.garment_id not in seen:
            chosen.append(g)
            seen.add(g.garment_id)
    rng.shuffle(chosen)
    return chosen


def generate_candidates(
    pool: CandidatePool, ctx: OutfitContext, *, seed: int = 0
) -> list[tuple[ScoredGarment, ...]]:
    """Assemble valid outfits around each anchor.

    Only combinations that PASS the slot rules are returned. Scoring an invalid
    outfit is wasted work, and the rule evaluator is far cheaper than the
    scorer.
    """
    rng = random.Random(seed)
    anchors = _pick_anchors(pool, ctx, rng)
    if not anchors:
        return []

    candidates: list[tuple[ScoredGarment, ...]] = []
    seen_hashes: set[str] = set()
    optional = [s for s in optional_slots() if pool.by_slot.get(s)]

    for anchor in anchors:
        for structure in base_structures():
            if anchor.slot not in structure:
                continue
            # Fill the rest of this base structure, then optionally add one
            # layer/accessory. Deeper optional combinations explode the count
            # without improving the top of the ranking.
            others = [s for s in structure if s != anchor.slot]
            option_lists: list[list[ScoredGarment]] = []
            for slot in others:
                items = pool.by_slot.get(slot, [])[:COMPLEMENTS_PER_SLOT]
                if not items:
                    option_lists = []
                    break
                option_lists.append(items)
            if not option_lists and others:
                continue
            for slot in required_slots():
                items = pool.by_slot.get(slot, [])[:COMPLEMENTS_PER_SLOT]
                if not items:
                    option_lists = []
                    break
                option_lists.append(items)

            # PREFERRED SLOTS: filled when the wardrobe can, SKIPPED when it
            # cannot — the difference between "no shoes, so no outfits" and
            # "no shoes, so outfits without shoes".
            #
            # Appended to the same product as the required slots rather than
            # to `extras`, deliberately. `extras` adds at most ONE optional
            # garment to a combination, so routing footwear through it would
            # make shoes compete with a bag for the same slot in the product —
            # and would produce shoeless variants alongside shod ones for
            # someone who owns shoes, which is exactly what `prefer_one`
            # exists to avoid.
            for slot in preferred_slots():
                items = pool.by_slot.get(slot, [])[:COMPLEMENTS_PER_SLOT]
                if items:
                    option_lists.append(items)

            if not option_lists:
                continue

            for combination in itertools.product(*option_lists):
                base = (anchor, *combination)
                extras: list[tuple[ScoredGarment, ...]] = [()]
                extras += [(g,) for s in optional for g in pool.by_slot[s][:2]]
                for extra in extras:
                    # NOT named `items`: that name is already bound to a list
                    # of pool garments above, and reusing it makes this a
                    # list-vs-tuple type error that mypy reports 100 lines away
                    # from the cause.
                    combo: tuple[ScoredGarment, ...] = base + extra
                    ids = [g.garment_id for g in combo]
                    if len(set(ids)) != len(ids):
                        continue
                    if not evaluate([g.as_item() for g in combo]).valid:
                        continue
                    h = garment_set_hash(ids)
                    if h in seen_hashes:
                        continue
                    seen_hashes.add(h)
                    candidates.append(combo)
                    if len(candidates) >= MAX_CANDIDATES:
                        return candidates
    return candidates


async def load_style_vector(session: Any) -> tuple[Any | None, int]:
    """This tenant's style vector and how many events built it.

    ONE LOADER, THREE CALL SITES, and that is the point. The nightly
    precompute and the two live paths must score identically or the precompute
    serves rankings the request path would not reproduce — the same class of
    bug as `serve_partial_cache` in the reranker. A vector loaded differently
    in one of the three is indistinguishable from a scoring regression.

    Returns (None, 0) when the user has no vector yet, which is the common case
    and not an error: `style_affinity` reports it as uninformative and
    contributes zero rather than guessing.
    """
    import numpy as np

    row = await session.execute(
        text("SELECT vector::text AS v, events_applied FROM user_style_vector LIMIT 1")
    )
    found = row.mappings().one_or_none()
    if found is None:
        return None, 0
    parsed = parse_embedding(found["v"])
    if not parsed:
        return None, int(found["events_applied"] or 0)
    return np.asarray(parsed, dtype=np.float64), int(found["events_applied"] or 0)


def diversify(
    scored: list[tuple[tuple[ScoredGarment, ...], OutfitScore]],
    *,
    limit: int,
    overlap_penalty: float,
) -> list[tuple[tuple[ScoredGarment, ...], OutfitScore]]:
    """Pick `limit` outfits that are individually good AND different from each other.

    THE PROBLEM THIS SOLVES IS A PROPERTY OF THE LIST, NOT OF ANY OUTFIT.
    `score_outfit` judges one outfit at a time, so a wardrobe with one strong
    shirt yields a top three that is the same shirt three times with the shoes
    swapped. Every entry is correctly scored; the list still offers one
    decision dressed up as three.

    GREEDY, WITH A PENALTY, NOT A HARD BUDGET. A per-garment appearance cap
    ("no item twice in the top five") is easier to explain and wrong on a small
    wardrobe: with ten garments nearly every outfit shares something, so a cap
    either empties the list or forces genuinely bad outfits into it. A penalty
    degrades instead — when everything overlaps, every candidate is penalised
    alike and the original ranking survives.

    The penalty is measured against the WORST offender among the already-picked
    outfits, not the average. Repeating one garment from rank 1 is the thing a
    reader notices; averaging it against four unrelated outfits would dilute
    exactly the signal that matters.

    DETERMINISTIC. The nightly precompute and the request path must produce the
    same order or `served_from: materialised` and `served_from: live` disagree,
    which is the bug the precompute design exists to avoid. Ties break on the
    garment-set hash, the same way `suggest` already breaks them.
    """
    if overlap_penalty <= 0 or limit <= 1:
        return scored[:limit]

    remaining = list(scored)
    picked: list[tuple[tuple[ScoredGarment, ...], OutfitScore]] = []
    picked_sets: list[frozenset[str]] = []

    while remaining and len(picked) < limit:
        best_index = 0
        best_key: tuple[float, str] | None = None
        for index, (items, result) in enumerate(remaining):
            ids = frozenset(g.garment_id for g in items)
            worst = max(
                (len(ids & seen) / len(ids) for seen in picked_sets),
                default=0.0,
            )
            adjusted = result.total - overlap_penalty * worst
            # Negated score first so a higher score sorts earlier, then the
            # set hash for a stable tie-break.
            key = (-adjusted, garment_set_hash([g.garment_id for g in items]))
            if best_key is None or key < best_key:
                best_key, best_index = key, index
        chosen = remaining.pop(best_index)
        picked.append(chosen)
        picked_sets.append(frozenset(g.garment_id for g in chosen[0]))

    return picked


def suggest(
    pool: CandidatePool,
    ctx: OutfitContext,
    *,
    limit: int = 20,
    today: date | None = None,
    seed: int = 0,
    style_vector: Any | None = None,
    style_events: int = 0,
    trends: dict[tuple[str, str], float] | None = None,
) -> SuggestionResult:
    """Generate, score, rank. Deterministic for a given pool and seed.

    `style_vector` / `style_events` come from `user_style_vector` and are what
    make `style_affinity` non-zero. They are ARGUMENTS rather than something
    loaded in here so this function stays pure and the nightly precompute and
    the request path provably score the same way — the property the whole
    precompute design rests on.
    """
    candidates = generate_candidates(pool, ctx, seed=seed)
    scored: list[tuple[tuple[ScoredGarment, ...], OutfitScore]] = []
    for items in candidates:
        result = score_outfit(
            list(items),
            warmth_target=ctx.warmth_target,
            formality_target=ctx.formality_target,
            today=today,
            style_vector=style_vector,
            style_events=style_events,
            trends=trends,
        )
        if result.total > 0:
            scored.append((items, result))
    # Tie-break on the set hash, not on iteration order: two outfits with the
    # same score must rank identically across runs or the nightly precompute
    # and the request path disagree.
    scored.sort(key=lambda p: (-p[1].total, garment_set_hash([g.garment_id for g in p[0]])))

    # Variety across the list, applied at the TRUNCATION rather than inside the
    # score: an outfit's quality does not depend on what else is being shown,
    # and mixing the two would make `score_breakdown` unexplainable.
    diversity = load_scoring_config().get("diversity") or {}
    penalty = (
        float(diversity.get("overlap_penalty", 0.0))
        if diversity.get("enabled", False)
        else 0.0
    )
    return SuggestionResult(
        outfits=diversify(scored, limit=limit, overlap_penalty=penalty),
        candidates_considered=len(candidates),
        pool=pool,
    )
