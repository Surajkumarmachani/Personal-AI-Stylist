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


def style_affinity(
    garments: list[ScoredGarment], style_vector: np.ndarray | None = None
) -> SubScore:
    """Zero until Phase 8 builds the style vector.

    Present, weighted and reported at zero rather than omitted, so the weights
    in config are the real weights and `score_breakdown` has a stable shape.
    Omitting it would mean the other five weights secretly sum to 0.80 and
    every score would be depressed by 20% for a reason no reader could see.
    """
    if style_vector is None:
        return SubScore(0.0, False, "style vector arrives in Phase 8 (weighted 0.20)")
    return SubScore(0.0, False, "style vector present but scoring lands in Phase 8")


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


def trend_alignment(garments: list[ScoredGarment]) -> SubScore:
    """Zero until Phase 11. Same reasoning as style_affinity."""
    return SubScore(0.0, False, "trend signals arrive in Phase 11 (weighted 0.05)")


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
) -> OutfitScore:
    """The weighted total, plus a breakdown you can debug a ranking from."""
    cfg = load_scoring_config()
    weights = {k: float(v) for k, v in cfg["weights"].items()}
    inactive = set(cfg.get("inactive_until_later_phases", []))

    rule_result = evaluate([g.as_item() for g in garments])

    subs: dict[str, SubScore] = {
        "colour_harmony": colour_harmony(garments),
        "formality_coherence": formality_coherence(garments, formality_target),
        "weather_fit": weather_fit(garments, warmth_target),
        "style_affinity": style_affinity(garments, style_vector),
        "novelty": novelty(garments, today),
        "trend_alignment": trend_alignment(garments),
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
