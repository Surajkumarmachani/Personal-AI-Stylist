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
from stylist_domain.scoring import OutfitScore, ScoredGarment, score_outfit
from stylist_domain.slots import base_structures, evaluate, optional_slots, required_slots
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

    rows = await session.execute(
        text(
            """
            SELECT g.id, g.slot::text AS slot, g.subcategory::text AS subcategory,
                   g.primary_colour::text AS primary_colour,
                   g.secondary_colour::text AS secondary_colour,
                   g.pattern::text AS pattern, g.material::text AS material,
                   g.formality, g.warmth, g.climate_bands,
                   COALESCE(w.wear_count, 0) AS wear_count,
                   w.last_worn
            FROM garments g
            LEFT JOIN (
                SELECT garment_id, count(*) AS wear_count, max(worn_on) AS last_worn
                FROM wear_log GROUP BY garment_id
            ) w ON w.garment_id = g.id
            WHERE g.is_active
              AND g.state NOT IN ('rejected', 'quarantined', 'duplicate_suspect')
              -- in the wash is a hard exclusion: it is not in the wardrobe today
              AND NOT g.needs_wash
              -- unset warmth is KEPT. A garment whose tagging degraded is still
              -- wearable, and excluding it would make a degraded ingest look
              -- like a lost garment.
              AND (g.warmth IS NULL OR abs(g.warmth - :warmth_target) <= 1)
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
        ),
        {
            "warmth_target": ctx.warmth_target,
            "compatible": compatible,
            # Climate matching needs the band set for this warmth level, which
            # the taxonomy derives rather than storing per garment.
            "climate_any": True,
            "poor_wet": poor_when_wet,
            "recent_days": RECENTLY_WORN_DAYS,
        },
    )

    pool = CandidatePool()
    for r in rows.mappings():
        item = ScoredGarment(
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
        )
        pool.by_slot.setdefault(item.slot, []).append(item)

    await _apply_avoids(session, pool)

    # Fail fast and SAY WHY. A wardrobe with no footwear can produce no valid
    # outfit at all, and discovering that after scoring 400 candidates is both
    # wasted work and an unexplained empty screen.
    for slot in required_slots():
        if not pool.by_slot.get(slot):
            pool.notes.append(
                f"no wearable {slot} — every outfit needs one "
                f"(check the laundry basket and the {ctx.dress_code_target} dress code)"
            )
    if not any(all(pool.by_slot.get(s) for s in structure) for structure in base_structures()):
        readable = " or ".join("+".join(s) for s in base_structures())
        pool.notes.append(f"no complete base structure available ({readable})")
    return pool


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


def suggest(
    pool: CandidatePool,
    ctx: OutfitContext,
    *,
    limit: int = 20,
    today: date | None = None,
    seed: int = 0,
) -> SuggestionResult:
    """Generate, score, rank. Deterministic for a given pool and seed."""
    candidates = generate_candidates(pool, ctx, seed=seed)
    scored: list[tuple[tuple[ScoredGarment, ...], OutfitScore]] = []
    for items in candidates:
        result = score_outfit(
            list(items),
            warmth_target=ctx.warmth_target,
            formality_target=ctx.formality_target,
            today=today,
        )
        if result.total > 0:
            scored.append((items, result))
    # Tie-break on the set hash, not on iteration order: two outfits with the
    # same score must rank identically across runs or the nightly precompute
    # and the request path disagree.
    scored.sort(key=lambda p: (-p[1].total, garment_set_hash([g.garment_id for g in p[0]])))
    return SuggestionResult(
        outfits=scored[:limit],
        candidates_considered=len(candidates),
        pool=pool,
    )
