"""Calendar event title -> occasion, with a confidence gate (Phase 8).

THE GATE IS THE FEATURE
-----------------------
The plan states the rule plainly: below the threshold, "fall back to default
AND SAY SO IN THE UI. A confidently wrong occasion is worse than asking."

So `classify()` never returns a bare answer. It returns what matched, how sure
it is, and whether that cleared the floor — and the caller cannot render the
occasion without also having the evidence, because they arrive together. An API
that returned only `occasion` would make "say so" an optional extra that the
first UI iteration quietly drops.

PURE, AND DELIBERATELY NOT A MODEL
-----------------------------------
A keyword table classifies worse than an LLM and can explain itself, which is
what the gate requires: "matched 'standup' -> office_casual" is something a UI
can show and a user can correct. "The model said 0.62" is not. It also keeps
calendar sync off the paid path and out of the 1500ms budget entirely.

Event titles are USER DATA and are treated as such: nothing here logs a title,
and `MatchedRule.phrase` carries only the phrase WE matched, never the
surrounding text the user wrote.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from stylist_domain.taxonomy import load_taxonomy

RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "calendar_rules.yaml"


@dataclass(frozen=True)
class MatchedRule:
    """One phrase that fired. Shown to the user, so it carries no event text."""

    occasion: str
    phrase: str
    confidence: float


@dataclass(frozen=True)
class Classification:
    """The answer AND its justification, inseparably.

    `confident` is the gate. When it is False the caller must present
    `occasion` as a fallback the user can change, not as a finding — which is
    why `is_fallback` exists as its own flag rather than being inferred from a
    number the UI would have to know the threshold for.
    """

    occasion: str
    dress_code: str
    formality_target: int
    confidence: float
    confident: bool
    is_fallback: bool
    matches: tuple[MatchedRule, ...]

    @property
    def explanation(self) -> str:
        """One line a UI can show verbatim. No event text, ever."""
        if self.is_fallback:
            if self.matches:
                best = self.matches[0]
                return (
                    f"Not sure — '{best.phrase}' suggests {best.occasion} but only "
                    f"{best.confidence:.0%}. Defaulting to {self.occasion}."
                )
            return f"Nothing in your calendar suggested an occasion. Defaulting to {self.occasion}."
        return f"Matched '{self.matches[0].phrase}' → {self.occasion}."


@lru_cache(maxsize=1)
def load_rules() -> dict[str, Any]:
    """Rules, validated against the taxonomy at load.

    A rule naming an occasion the taxonomy does not define would classify an
    event into a value the context resolver then rejects — surfacing as a 500
    on a calendar sync rather than as the typo it is. Checked here, once,
    rather than at every call.
    """
    raw = yaml.safe_load(RULES_PATH.read_text())
    known = {o["id"] for o in load_taxonomy().raw["occasions"]}

    unknown = sorted({r["occasion"] for r in raw["rules"]} - known)
    if unknown:
        raise ValueError(f"calendar_rules.yaml names occasions not in taxonomy.yaml: {unknown}")
    if raw["default_occasion"] not in known:
        raise ValueError(f"default_occasion {raw['default_occasion']!r} is not in taxonomy.yaml")
    if not 0.0 < float(raw["confidence_floor"]) <= 1.0:
        raise ValueError("confidence_floor must be in (0, 1]")
    return dict(raw)


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    # WORD BOUNDARIES, not substring. "interview" as a substring matches
    # "winterview" and "reinterview"; "run" matches "brunch" and "runway".
    # Multi-word phrases allow flexible whitespace so "stand  up" still hits.
    parts = [re.escape(p) for p in phrase.split()]
    return re.compile(r"\b" + r"\s+".join(parts) + r"\b", re.IGNORECASE)


@lru_cache(maxsize=1)
def _compiled() -> tuple[tuple[str, str, float, re.Pattern[str]], ...]:
    rules = load_rules()
    out = []
    for rule in rules["rules"]:
        for phrase in rule["phrases"]:
            out.append(
                (rule["occasion"], phrase, float(rule["confidence"]), _phrase_pattern(phrase))
            )
    # Longest phrase first, so "black tie" wins over a bare "tie" and
    # "client call" over "call". Without this the answer would depend on YAML
    # ordering, which is not something a reviewer of that file would think to
    # check.
    return tuple(sorted(out, key=lambda t: -len(t[1])))


def classify(titles: list[str]) -> Classification:
    """Classify today's event titles into one occasion.

    Several events in a day is the normal case, so the DRESSIEST confident
    match wins rather than the first or the most frequent: you have to dress
    for the wedding even if it is one event among six, and nobody changes
    clothes between a standup and a client meeting. Formality is the ordering,
    taken from the taxonomy rather than from the rule table, so "which is
    dressier" stays a single definition.
    """
    rules = load_rules()
    taxonomy = load_taxonomy()
    by_id = {o["id"]: o for o in taxonomy.raw["occasions"]}
    floor = float(rules["confidence_floor"])

    matches: list[MatchedRule] = []
    for title in titles:
        for occasion, phrase, confidence, pattern in _compiled():
            if pattern.search(title):
                matches.append(MatchedRule(occasion, phrase, confidence))

    # Sort by confidence, then by formality: between two equally-sure matches,
    # the dressier one is the safer error. Being overdressed for a coffee is
    # recoverable; being underdressed at a wedding is not.
    matches.sort(
        key=lambda m: (-m.confidence, -int(by_id[m.occasion]["formality_target"]), m.occasion)
    )

    confident = [m for m in matches if m.confidence >= floor]
    if confident:
        # Among those that cleared the gate, the DRESSIEST wins.
        best = max(
            confident,
            key=lambda m: (int(by_id[m.occasion]["formality_target"]), m.confidence),
        )
        occasion = best.occasion
        return Classification(
            occasion=occasion,
            dress_code=by_id[occasion]["dress_code"],
            formality_target=int(by_id[occasion]["formality_target"]),
            confidence=best.confidence,
            confident=True,
            is_fallback=False,
            matches=(best, *[m for m in matches if m is not best]),
        )

    fallback = rules["default_occasion"]
    return Classification(
        occasion=fallback,
        dress_code=by_id[fallback]["dress_code"],
        formality_target=int(by_id[fallback]["formality_target"]),
        confidence=matches[0].confidence if matches else 0.0,
        confident=False,
        is_fallback=True,
        matches=tuple(matches),
    )
