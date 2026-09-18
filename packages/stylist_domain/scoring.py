"""Step 6.3 — the deterministic outfit scorer. No model calls.

    score = (0.30 * colour_harmony
           + 0.20 * formality_coherence
           + 0.15 * weather_fit
           + 0.20 * style_affinity      # zeros until Phase 8
           + 0.10 * novelty
           + 0.05 * trend_alignment)    # zeros until Phase 11
           * hard_penalty

EVERY SUB-SCORE RETURNS 0..1, AND EVERY ONE IS SEPARATELY TESTED
----------------------------------------------------------------
A weighted sum is only debuggable if the terms are commensurable. A sub-score
that can return 1.4 or -0.2 makes the weights lie about their influence, and
the failure is invisible because the total is still a number.

THE BREAKDOWN FLAGS DEGENERATE INPUTS
-------------------------------------
This matters more than it sounds. Right now `formality` and `warmth` come from
a mock that returns the same value for every garment, so `formality_coherence`
computes variance over a constant and returns the same number for every
outfit. It ranks nothing.

Without a flag, a bad suggestion is unattributable: you cannot tell whether
the scorer is wrong or its input was uniform. That exact ambiguity — measuring
the harness instead of the system — has already cost this project a long
detour. So each sub-score reports whether its input carried any signal, and
`score_breakdown` is persisted as JSONB because ranking cannot be debugged
after the fact without it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from stylist_domain.colour import delta_e_2000, hex_to_lab
from stylist_domain.slots import OutfitItem, evaluate
from stylist_domain.taxonomy import load_taxonomy

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "scoring.yaml"


@lru_cache(maxsize=1)
def load_scoring_config(path: str | None = None) -> dict[str, Any]:
    target = Path(path) if path else CONFIG_PATH
    data = yaml.safe_load(target.read_text())
    weights = data["weights"]
    total = sum(float(v) for v in weights.values())
    if abs(total - 1.0) > 1e-6:
        # Weights that do not sum to 1 make the total uninterpretable: a
        # "0.62" would not mean 62% of anything, and comparing two outfits
        # scored under different config versions would be meaningless.
        raise ValueError(f"scoring weights must sum to 1.0, got {total}")
    return dict(data)


@dataclass(frozen=True)
class ScoredGarment:
    """What the scorer needs. Deliberately not the ORM row.

    The scorer is pure and property-testable precisely because it takes this
    and not a database object.
    """

    garment_id: str
    slot: str
    subcategory: str
    primary_colour: str | None = None
    secondary_colour: str | None = None
    pattern: str | None = None
    material: str | None = None
    formality: int | None = None
    warmth: int | None = None
    wear_count: int = 0
    last_worn: date | None = None
    # The garment's SigLIP vector, when the caller loaded it. Optional because
    # most of the scorer does not need it and every other sub-score stays pure
    # arithmetic on tags — only `style_affinity` reads it.
    embedding: tuple[float, ...] | None = None

    def as_item(self) -> OutfitItem:
        return OutfitItem(self.garment_id, self.slot, self.subcategory)


@dataclass(frozen=True)
class SubScore:
    value: float
    # False when the input could not distinguish this outfit from any other —
    # a constant field, or a field that is entirely absent.
    informative: bool
    detail: str = ""


@dataclass(frozen=True)
class OutfitScore:
    total: float
    breakdown: dict[str, Any] = field(default_factory=dict)
    violations: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.violations


# --------------------------------------------------------------- sub-scores


def _neutral(colour: str | None) -> bool:
    if colour is None:
        return True  # An unknown colour must not be treated as a clash.
    cfg = load_scoring_config()
    family_name = cfg["colour_harmony"]["neutral_family_name"]
    return bool(load_taxonomy().colour_family.get(colour) == family_name)


def _lab(colour: str) -> np.ndarray | None:
    hexv = load_taxonomy().colour_hex.get(colour)
    return hex_to_lab(hexv) if hexv else None


def colour_harmony(garments: list[ScoredGarment]) -> SubScore:
    """Pairwise CIEDE2000 over the non-neutral colours, neutrals as anchors.

    The shape of the judgement: very close reads as deliberate monochrome, far
    apart reads as deliberate contrast, and the middle is where two colours
    look like an accident rather than a choice. Neutrals are excluded from the
    clash measurement because black does not clash with anything — including
    them would score a black-and-red outfit as a mid-distance collision.
    """
    cfg = load_scoring_config()["colour_harmony"]
    mono = float(cfg["monochrome_below_delta_e"])
    contrast = float(cfg["contrast_above_delta_e"])
    floor = float(cfg["awkward_band_score"])

    colours = [g.primary_colour for g in garments if g.primary_colour]
    if not colours:
        return SubScore(0.5, False, "no colours on any garment")

    chromatic = [c for c in colours if not _neutral(c)]
    if len(chromatic) <= 1:
        # Zero or one chromatic colour against neutrals always works. This is
        # the most common real outfit and it should not be scored as "unknown".
        return SubScore(1.0, True, f"{len(chromatic)} chromatic colour with neutral anchors")

    labs = [(c, _lab(c)) for c in chromatic]
    usable = [(c, v) for c, v in labs if v is not None]
    if len(usable) <= 1:
        return SubScore(0.5, False, "colours have no hex in the taxonomy")

    scores: list[float] = []
    pairs: list[str] = []
    for i in range(len(usable)):
        for j in range(i + 1, len(usable)):
            (ca, la), (cb, lb) = usable[i], usable[j]
            de = delta_e_2000(la, lb)
            if de < mono:
                pair_score = 1.0
            elif de > contrast:
                pair_score = 0.85
            else:
                # Linear dip through the awkward band, worst at its midpoint.
                midpoint = (mono + contrast) / 2
                closeness = 1 - abs(de - midpoint) / ((contrast - mono) / 2)
                pair_score = 1.0 - (1.0 - floor) * closeness
            scores.append(pair_score)
            pairs.append(f"{ca}~{cb}:dE{de:.1f}")

    saturated = len(usable)
    cap = int(cfg["max_saturated_hues"])
    value = sum(scores) / len(scores)
    detail = f"{saturated} chromatic, pairs {', '.join(pairs[:4])}"
    if saturated > cap:
        detail += f" (over the {cap}-hue threshold)"
    return SubScore(round(min(1.0, max(0.0, value)), 4), True, detail)


def formality_coherence(garments: list[ScoredGarment], target: int) -> SubScore:
    """Penalise spread across items, and distance from the occasion's target.

    Both halves matter: five items all at formality 2 are coherent but wrong
    for an interview, and an outfit averaging 4 built from a 1 and a 5 is
    neither coherent nor wearable.
    """
    values = [g.formality for g in garments if g.formality is not None]
    if not values:
        return SubScore(0.5, False, "no formality on any garment")
    if len(set(values)) == 1 and len(values) > 1:
        # THE MOCK CASE. Every garment carries the same formality, so spread is
        # structurally 0 and this sub-score cannot separate any two outfits.
        # Reported as uninformative rather than as a good score.
        distance = abs(values[0] - target)
        return SubScore(
            round(max(0.0, 1.0 - distance / 4.0), 4),
            False,
            f"all {len(values)} garments have formality {values[0]} — "
            "no spread signal (mock or unset tags)",
        )

    spread = max(values) - min(values)
    mean = sum(values) / len(values)
    # Spread of 0 is perfect, 4 is the worst possible on a 1-5 scale.
    spread_score = 1.0 - spread / 4.0
    target_score = 1.0 - abs(mean - target) / 4.0
    value = 0.5 * spread_score + 0.5 * target_score
    return SubScore(
        round(min(1.0, max(0.0, value)), 4),
        True,
        f"spread {spread}, mean {mean:.1f} vs target {target}",
    )


def weather_fit(garments: list[ScoredGarment], warmth_target: int) -> SubScore:
    """How close the outfit's warmth sits to what the weather asks for."""
    cfg = load_scoring_config()["weather_fit"]
    tolerance = float(cfg["warmth_tolerance"])
    values = [g.warmth for g in garments if g.warmth is not None]
    if not values:
        return SubScore(0.5, False, "no warmth on any garment")
    if len(set(values)) == 1 and len(values) > 1:
        distance = abs(values[0] - warmth_target)
        return SubScore(
            round(max(0.0, 1.0 - distance / 4.0), 4),
            False,
            f"all garments have warmth {values[0]} — no signal (mock or unset tags)",
        )

    # The warmest item drives how warm an outfit actually is: a coat over a
    # t-shirt is a warm outfit. A mean would let thin items cancel a coat.
    effective = max(values)
    distance = abs(effective - warmth_target)
    value = 1.0 if distance <= tolerance else max(0.0, 1.0 - (distance - tolerance) / 4.0)
    return SubScore(round(value, 4), True, f"warmest item {effective} vs target {warmth_target}")


# Below this many feedback events the style vector is noise, not taste.
#
# EWMA with alpha=0.1 has ~20 reactions of effective memory, so at 3 events the
# vector is dominated by whichever outfit happened to be rated first. Scoring
# against it would hand 20% of the weight to an accident and — worse — the
# accident would be self-reinforcing, because the outfits it favours are the
# ones that get shown and rated next.
#
# PROVISIONAL — set 2026-09-18 with no real feedback events in the system.
# Resolves when: ~100 real events exist and the vector can be checked against
# held-out reactions instead of against reasoning.
MIN_EVENTS_FOR_STYLE_AFFINITY = 10


def style_affinity(
    garments: list[ScoredGarment],
    style_vector: np.ndarray | None = None,
    *,
    events_applied: int = 0,
) -> SubScore:
    """How close this outfit sits to the taste the user has demonstrated.

    WIRED 2026-09-18, AND IT WAS DEAD BEFORE THAT. Phase 8 built the style
    vector — EWMA, replayable, `user_style_vector` — and the feedback router
    writes it on every reaction. Nothing ever read it. This function returned
    0.0 unconditionally, including when handed a vector, and the suggest
    pipeline never passed one anyway. At weight 0.20 that is a fifth of the
    score permanently zero, with `trend_alignment` making it a quarter.

    That is the recurring failure in this codebase under a new name: a signal
    that looks live — a populated table, a tested pure function, a weight in
    config — and cannot affect the thing it exists to affect.

    COSINE CLAMPED AT ZERO, not rescaled to [0,1]. `(cos+1)/2` would hand every
    outfit 0.5 for being merely orthogonal to the user's taste, which is 0.10
    of free score for no information, and would make an outfit the user
    demonstrably dislikes (negative cosine) still outscore nothing. Clamping
    also keeps this continuous with the unwired behaviour: no vector and an
    unlike-you outfit both score 0.
    """
    if style_vector is None:
        return SubScore(0.0, False, "no style vector for this user yet (weighted 0.20)")
    if events_applied < MIN_EVENTS_FOR_STYLE_AFFINITY:
        return SubScore(
            0.0,
            False,
            f"style vector has {events_applied} event(s), needs "
            f"{MIN_EVENTS_FOR_STYLE_AFFINITY} before it is trusted",
        )

    embeddings = [list(g.embedding) for g in garments if g.embedding]
    if not embeddings:
        # Garments predating the embed stage, or a pool loaded without
        # embeddings. Reported rather than silently scored as dissimilar.
        return SubScore(0.0, False, "no garment embeddings in this outfit")

    from stylist_domain.style import outfit_embedding

    outfit_vec = outfit_embedding(embeddings)
    if outfit_vec is None:
        return SubScore(0.0, False, "outfit embedding could not be computed")

    sv = np.asarray(style_vector, dtype=np.float64)
    if sv.shape[0] != len(outfit_vec):
        raise ValueError(f"dimension mismatch: style {sv.shape[0]} vs outfit {len(outfit_vec)}")
    norm = float(np.linalg.norm(sv))
    if norm == 0.0:
        # A real state: equal likes and dislikes converge here. Not an error,
        # and not similarity either.
        return SubScore(0.0, False, "style vector is zero — no net preference yet")

    cosine = float(np.dot(sv / norm, np.asarray(outfit_vec, dtype=np.float64)))
    value = max(0.0, cosine)
    return SubScore(
        value,
        True,
        f"cosine {cosine:+.3f} against a style vector from {events_applied} event(s)"
        + (" (clamped to 0)" if cosine < 0 else ""),
    )


def novelty(garments: list[ScoredGarment], today: date | None = None) -> SubScore:
    """Reward what has not been worn lately; damp raw popularity.

    Log damping because wear counts follow a power law — one favourite shirt
    worn 60 times would otherwise dominate every ranking on a linear scale.
    """
    cfg = load_scoring_config()["novelty"]
    window = int(cfg["recency_window_days"])
    today = today or date.today()

    if not garments:
        return SubScore(0.5, False, "no garments")

    recency_scores: list[float] = []
    for g in garments:
        if g.last_worn is None:
            recency_scores.append(1.0)  # never worn is maximally novel
            continue
        days = (today - g.last_worn).days
        recency_scores.append(min(1.0, max(0.0, days / window)))

    counts = [g.wear_count for g in garments]
    if cfg.get("log_damping", True):
        damped = [1.0 / (1.0 + math.log1p(c)) for c in counts]
    else:
        damped = [1.0 / (1.0 + c) for c in counts]

    value = 0.6 * (sum(recency_scores) / len(recency_scores)) + 0.4 * (sum(damped) / len(damped))
    informative = any(g.last_worn is not None or g.wear_count for g in garments)
    return SubScore(
        round(min(1.0, max(0.0, value)), 4),
        informative,
        f"mean recency {sum(recency_scores) / len(recency_scores):.2f}, "
        f"wear counts {counts}" + ("" if informative else " — nothing has ever been worn"),
    )


# Which fields carry a trend at all.
#
# `subcategory`, `primary_colour`, `pattern` and `material` are the fields where
# "more people are wearing this lately" is a meaningful statement. `formality`
# and `warmth` are deliberately absent: they are properties of the OCCASION and
# the WEATHER, already scored by their own terms, and a "trend toward warmer
# clothes" is a season, not a taste.
TREND_FIELDS = ("subcategory", "primary_colour", "pattern", "material")


def trend_alignment(
    garments: list[ScoredGarment], trends: dict[tuple[str, str], float] | None = None
) -> SubScore:
    """How much of this outfit is in something people are wearing more lately.

    WIRED 2026-09-18 (Phase 11). First-party only: the signal is our own wear
    logs — which values are being worn above their own recent baseline — never
    a scraped or unlicensed trend feed. Phase 11's constraint is "licensed or
    first-party sources only, capped at <=10% of score", and the weight is
    0.05, which satisfies the cap with room.

    ONLY PUBLISHED TRENDS COUNT. `trends` holds the rows that passed the
    k-anonymity floor in `trend_signal`; a value not in it scores 0 rather than
    being guessed at. So a wardrobe full of garments nobody else owns gets no
    trend credit, which is correct — there is no trend to be aligned with.

    AVERAGED OVER GARMENTS, NOT SUMMED. A five-piece outfit must not outscore
    a three-piece one for being larger, and the weighted sum only stays
    debuggable if every sub-score is a [0,1] intensity rather than a total.
    """
    if not trends:
        return SubScore(0.0, False, "no published trend signals (weighted 0.05)")

    scores: list[float] = []
    matched: list[str] = []
    for g in garments:
        values = {
            "subcategory": g.subcategory,
            "primary_colour": g.primary_colour,
            "pattern": g.pattern,
            "material": g.material,
        }
        best = 0.0
        best_label = ""
        for field_name in TREND_FIELDS:
            value = values.get(field_name)
            if not value:
                continue
            found = trends.get((field_name, value))
            # The STRONGEST matching field, not the sum: a garment that is both
            # a trending colour and a trending material is one garment, and
            # adding the two would let a single item carry the whole outfit.
            if found is not None and found > best:
                best, best_label = found, f"{field_name}={value}"
        scores.append(best)
        if best_label:
            matched.append(best_label)

    if not scores:
        return SubScore(0.0, False, "no garments to score")

    mean_trend = sum(scores) / len(scores)
    informative = bool(matched)
    return SubScore(
        round(min(1.0, max(0.0, mean_trend)), 4),
        informative,
        (f"{len(matched)}/{len(scores)} garment(s) in a published trend: " + ", ".join(matched[:4]))
        if informative
        else "no garment in this outfit matched a published trend",
    )


# ------------------------------------------------------------ hard penalty


def hard_penalty(garments: list[ScoredGarment]) -> tuple[float, list[str]]:
    """Multiplicative penalty from taxonomy's soft rules.

    Multiplicative, not subtractive: an outfit breaking three rules should be
    pushed far down rather than merely offset, and the result cannot go
    negative — a negative score would sort above nothing and break ranking.
    """
    rules = load_taxonomy().raw["outfit_rules"]
    bold = set(load_taxonomy().bold_patterns)
    cfg = load_scoring_config()["colour_harmony"]

    applied: list[str] = []
    multiplier = 1.0
    for penalty in rules.get("penalties", []):
        name = penalty["rule"]
        weight = float(penalty["weight"])  # negative
        triggered = False

        if name == "more_than_two_saturated_hues":
            chromatic = [g.primary_colour for g in garments if not _neutral(g.primary_colour)]
            triggered = len(chromatic) > int(cfg["max_saturated_hues"])
        elif name == "more_than_two_bold_patterns":
            # Read from taxonomy's bold set, never a hand-listed copy.
            triggered = sum(1 for g in garments if g.pattern in bold) > 2
        elif name == "formality_spread_gt_2":
            values = [g.formality for g in garments if g.formality is not None]
            triggered = bool(values) and (max(values) - min(values)) > 2

        if triggered:
            multiplier *= 1.0 + weight
            applied.append(f"{name} ({weight:+.2f})")

    return max(0.0, round(multiplier, 4)), applied


# ------------------------------------------------------------------ total


def score_outfit(
    garments: list[ScoredGarment],
    *,
    warmth_target: int,
    formality_target: int,
    today: date | None = None,
    style_vector: np.ndarray | None = None,
    style_events: int = 0,
    trends: dict[tuple[str, str], float] | None = None,
) -> OutfitScore:
    """The weighted total, plus a breakdown you can debug a ranking from.

    `style_events` gates `style_affinity`: a vector built from three reactions
    is an accident, and scoring against it would make the accident
    self-reinforcing because the outfits it favours are the ones shown next.
    """
    cfg = load_scoring_config()
    weights = {k: float(v) for k, v in cfg["weights"].items()}
    inactive = set(cfg.get("inactive_until_later_phases", []))

    rule_result = evaluate([g.as_item() for g in garments])

    subs: dict[str, SubScore] = {
        "colour_harmony": colour_harmony(garments),
        "formality_coherence": formality_coherence(garments, formality_target),
        "weather_fit": weather_fit(garments, warmth_target),
        "style_affinity": style_affinity(garments, style_vector, events_applied=style_events),
        "novelty": novelty(garments, today),
        "trend_alignment": trend_alignment(garments, trends),
    }

    for name, sub in subs.items():
        if not (0.0 <= sub.value <= 1.0):
            # A sub-score outside 0..1 makes the weights lie about influence.
            raise AssertionError(f"{name} returned {sub.value}, outside 0..1")

    weighted = sum(weights[name] * sub.value for name, sub in subs.items())
    penalty, applied = hard_penalty(garments)
    total = round(weighted * penalty, 6)

    # An outfit that breaks a HARD rule scores zero rather than being ranked
    # low: the plan says such combinations are "never surfaced", and a low
    # score still surfaces when the wardrobe is small.
    if not rule_result.valid:
        total = 0.0

    return OutfitScore(
        total=total,
        violations=rule_result.violations,
        breakdown={
            "config_version": cfg.get("version"),
            "weighted_sum": round(weighted, 6),
            "hard_penalty": penalty,
            "penalties_applied": applied,
            "rule_violations": list(rule_result.violations),
            "sub_scores": {
                name: {
                    "value": sub.value,
                    "weight": weights[name],
                    "contribution": round(weights[name] * sub.value, 6),
                    "informative": sub.informative,
                    "inactive_by_design": name in inactive,
                    "detail": sub.detail,
                }
                for name, sub in subs.items()
            },
            # The headline caveat, computed rather than remembered: how much of
            # the score came from inputs that could actually separate outfits.
            "informative_weight": round(
                sum(
                    weights[name]
                    for name, sub in subs.items()
                    if sub.informative and name not in inactive
                ),
                4,
            ),
        },
    )
