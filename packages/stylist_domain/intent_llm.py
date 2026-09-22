"""Free text -> an occasion, when the lexicon could not.

WHY THIS EXISTS ALONGSIDE `intent.py` RATHER THAN REPLACING IT
--------------------------------------------------------------
`intent.py` opens with three reasons not to use a model for this, and all
three are still right:

  reliability   a model asked to pick from 18 enum values will occasionally
                invent a nineteenth
  latency       the chat shares `/suggestions`' 1500ms budget; a 3-6s model
                call does not fit
  availability  a chat that cannot answer "what should I wear for Diwali"
                during a provider outage is a chat nobody trusts

This module does not argue with any of that. It is reached ONLY when the
lexicon returned nothing, and a lexicon miss today produces "I'm not sure what
the occasion is" — an answer of no value. Each objection is therefore paid for
rather than waived:

  reliability   `strict` json_schema with the taxonomy ids as an ENUM, AND a
                server-side check against the taxonomy afterwards. The schema
                is the polite request; `validate` is the gate — the same
                division rerank_schema.py sets out, for the same reason: a
                guarantee a provider can opt out of is not a guarantee.
  latency       only on a miss, on a hard timeout, and the fast path is
                untouched. "Diwali" still costs nothing and answers instantly.
  availability  every failure — timeout, outage, malformed answer, unknown id
                — degrades to exactly the clarification the chat shows today.
                No new hard dependency; strictly more answers than before.

WHY IT ALSO RETURNS ALTERNATIVES
--------------------------------
The failure the user actually reported was not a wrong answer, it was a dead
end: "I'm not sure what the occasion is" plus five fixed examples that had
nothing to do with what they had typed. So the model is asked for its two
next-best guesses even when it is unsure, and the clarification offers THOSE.
"Did you mean a casual outing or a travel day?" is a question someone can
answer; "Try: Diwali, a client meeting…" is a form letter.

`UNKNOWN` IS A REAL ANSWER AND IS IN THE ENUM
---------------------------------------------
"hello", "what's the weather", "who made you" are not occasions, and a model
forced to choose from 18 will pick one — confidently. Giving it an explicit
way to say "this is not about an occasion" is what keeps it from dressing a
greeting for a wedding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from stylist_domain.taxonomy import load_taxonomy

# The model's escape hatch. Not a taxonomy id — deliberately, so that a bug
# that let it through would fail `resolve_context` loudly rather than dress
# someone for a made-up occasion.
UNKNOWN = "unknown"

# Below this the answer is offered as a QUESTION, not acted on.
#
# 0.6, not 0.5. A coin-flip classification spends a suggestion request and
# shows the user an outfit for an occasion they did not name, which is the
# specific failure `resolve_context` refuses a default to avoid. Asking costs
# one more message; guessing wrong costs trust.
MIN_CONFIDENCE = 0.6

# The chat is a user-facing request. Longer than the reranker's 1200ms because
# this runs INSTEAD OF an answer rather than to improve one — but still capped,
# because a spinner that never resolves is worse than a clarifying question.
INTENT_TIMEOUT_S = 6.0

MODEL = "intent-classifier"


def occasion_ids() -> list[str]:
    """The taxonomy's occasion ids. Read, never hard-coded: a new occasion in
    taxonomy.yaml must become classifiable without editing this file, and a
    list duplicated here would silently stop matching the one the scorer uses.
    """
    return [str(o["id"]) for o in load_taxonomy().raw["occasions"]]


def build_schema() -> dict[str, Any]:
    """Strict JSON schema. The enum is the taxonomy plus UNKNOWN."""
    ids = [*occasion_ids(), UNKNOWN]
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "occasion_intent",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "occasion": {
                        "type": "string",
                        "enum": ids,
                        "description": (
                            "The single occasion this message is about. "
                            f"Use '{UNKNOWN}' if the message is not about "
                            "getting dressed for something."
                        ),
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": (
                            "How sure you are. Be honest rather than safe: below "
                            f"{MIN_CONFIDENCE} the user is ASKED instead of being "
                            "shown an outfit, which is the better outcome when you "
                            "are guessing."
                        ),
                    },
                    "alternatives": {
                        "type": "array",
                        "items": {"type": "string", "enum": ids},
                        "description": (
                            "Up to two OTHER occasions this could plausibly be, best "
                            "first. Offered to the user as a choice when confidence is "
                            "low, so fill these in even when you are unsure — "
                            "especially then."
                        ),
                    },
                },
                "required": ["occasion", "confidence", "alternatives"],
                "additionalProperties": False,
            },
        },
    }


SYSTEM_PROMPT = (
    "You map a person's message to ONE occasion from a fixed list, for a "
    "wardrobe app. Answer only with the schema.\n"
    "\n"
    "Judge what the person will be DOING, not where they are going. Sightseeing "
    "in another city is a casual outing; the flight there is a travel day. "
    "Meeting a partner's family is a dinner date or a casual outing, never a "
    "client meeting.\n"
    "\n"
    f"If the message is not about getting dressed for something, answer "
    f"'{UNKNOWN}'. Greetings, questions about the app, and small talk are "
    f"'{UNKNOWN}'. Do not stretch to fit."
)


@dataclass(frozen=True, slots=True)
class LLMIntent:
    occasion: str | None
    confidence: float
    alternatives: tuple[str, ...]

    @property
    def confident(self) -> bool:
        return self.occasion is not None and self.confidence >= MIN_CONFIDENCE


def validate(raw: Any) -> LLMIntent | None:
    """Gate the model's answer against the taxonomy. None if unusable.

    THE SCHEMA IS NOT THE GUARANTEE. `strict` is a request the provider may
    honour; some do not, and the free tier rotates between models that vary.
    An id that is not in the taxonomy would reach `resolve_context` and raise
    a ValueError inside a user-facing request, so it is rejected here instead.
    """
    if not isinstance(raw, dict):
        return None
    occasion = raw.get("occasion")
    if not isinstance(occasion, str):
        return None

    known = set(occasion_ids())
    if occasion == UNKNOWN:
        resolved: str | None = None
    elif occasion in known:
        resolved = occasion
    else:
        # An invented occasion. Not a soft failure — the whole point of the
        # enum. Falls through to the clarification, which is correct.
        return None

    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))

    alts = raw.get("alternatives")
    alternatives = tuple(
        a
        for a in (alts if isinstance(alts, list) else [])
        # Unknown ids dropped rather than rejecting the whole answer: a bad
        # SUGGESTION is cosmetic, unlike a bad classification.
        if isinstance(a, str) and a in known and a != resolved
    )[:2]

    return LLMIntent(occasion=resolved, confidence=confidence, alternatives=alternatives)
