"""Styling advice for an occasion this wardrobe cannot dress.

WHY THERE IS ANYTHING HERE AT ALL
---------------------------------
"Cultural wear" resolved correctly — festival day, festive ethnic — and the
user was told:

    I couldn't put together an outfit for the festival. no complete base
    structure available (upper_base+lower or full_body)

Two failures in one sentence. The second half was the schema's vocabulary
printed at a person, and `pipeline._base_structure_note` now fixes that: it
says which piece is missing and what to add. But even repaired it only
reports a gap, and a gap is not an answer. Someone who asked what to wear
still does not know what a festival outfit IS.

So this asks the model for the part the wardrobe cannot supply: what that
occasion is normally dressed in. It runs ONLY when the wardrobe produced no
outfits — the path that was already a dead end — so there is nothing to
regress, and every failure leaves the repaired note standing on its own.

WHAT IT MUST NOT DO
-------------------
CLAIM THE USER OWNS ANYTHING. The entire product rule is that outfits come
from the wardrobe that was catalogued; a model listing clothes the user may
not have, in the same bubble as real suggestions, would make the honest half
untrustworthy too. The prompt says so, the schema keeps it to one sentence,
and `validate` drops an answer that runs long.

It is advice, deliberately generic, and it is the difference between "I can't"
and "I can't, but here's what that occasion usually calls for".
"""

from __future__ import annotations

import json
import re
from typing import Any

# Same deployment as the intent classifier: a small, cheap, schema-obeying
# model with a local fallback already configured in `infra/litellm/config.yaml`.
# A second model name would be a second thing to keep credited and running for
# no benefit — the job is the same size.
MODEL = "intent-classifier"

# Shorter than the classifier's 6s. That call decides WHETHER there is an
# answer; this one only decorates one the user is already getting, so it must
# never be the reason a reply is slow.
ADVICE_TIMEOUT_S = 4.0

# One sentence. Long enough to name the garments, short enough that nobody
# mistakes it for the outfit list — which sits directly above it when the
# wardrobe can fill even part of the request.
MAX_ADVICE_CHARS = 240

SYSTEM_PROMPT = (
    "You advise on what an occasion is normally dressed in, for a wardrobe "
    "app in India. Answer only with the schema.\n"
    "\n"
    "You will be given an occasion and its dress code. Name, in ONE short "
    "sentence, the garments that occasion usually calls for -- be concrete "
    "('a kurta with churidar and juttis'), not abstract ('something "
    "traditional and festive').\n"
    "\n"
    "NEVER say or imply the person owns any of it. You cannot see their "
    "wardrobe. Write 'a festival usually calls for X', never 'wear your X' or "
    "'you could pair your X'. The app has already told them which piece they "
    "are missing; you are saying what the occasion looks like, nothing else.\n"
    "\n"
    "If you are told whose clothes to describe (womenswear or menswear), name "
    "ONLY garments from that line -- a lehenga is not advice for someone who "
    "wears menswear. If you are not told, name one option from each.\n"
    "\n"
    "No greeting, no apology, no offer to help further. One sentence."
)


def build_schema() -> dict[str, Any]:
    """Strict schema. One string, because one sentence is the whole contract."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "occasion_advice",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "advice": {
                        "type": "string",
                        "description": (
                            "One short sentence naming the garments this occasion "
                            "usually calls for. Never claims the user owns them."
                        ),
                    },
                },
                "required": ["advice"],
                "additionalProperties": False,
            },
        },
    }


# How `user_profile.dresses_as` is said to the model. 'all' and unasked are
# both absent: the prompt already covers "not told".
_LINE_WORDS = {"women": "womenswear", "men": "menswear"}


def build_user_message(
    occasion_phrase: str, dress_code: str, gap_note: str, dresses_as: str | None = None
) -> str:
    """What the model is told. The gap note is included so the advice can lean
    towards the missing piece rather than describing the whole outfit again.

    `dresses_as` is the user's stated answer (see migration 0026), and nothing
    else: the model is never asked to guess it from the wardrobe.
    """
    lines = [f"Occasion: {occasion_phrase}"]
    if dresses_as in _LINE_WORDS:
        lines.append(f"Describe: {_LINE_WORDS[dresses_as]}")
    if dress_code:
        lines.append(f"Dress code: {dress_code.replace('_', ' ')}")
    if gap_note:
        lines.append(f"What their wardrobe is missing: {gap_note}")
    return "\n".join(lines)


_SECOND_PERSON_RE = re.compile(r"\byou(?:r|rs|'ve|'ll|'d|'re)?\b", re.IGNORECASE)


def validate(raw: Any) -> str | None:
    """The advice, or None if it is unusable. Never raises.

    Rejects rather than truncates. A sentence cut mid-word reads like a bug,
    and this is an optional garnish — dropping it costs the user nothing they
    had before.
    """
    if not isinstance(raw, dict):
        return None
    advice = raw.get("advice")
    if not isinstance(advice, str):
        return None
    advice = " ".join(advice.split())
    if not advice or len(advice) > MAX_ADVICE_CHARS:
        return None

    # A model told not to claim ownership will still occasionally open with
    # "wear your...". Cheap to check, and the failure it prevents — inventing
    # a garment the user does not own — is the one that matters most here.
    #
    # ANY SECOND PERSON AT ALL, not a list of ownership phrases. The list was
    # tried first and leaked immediately: "You already have a great saree"
    # contains neither "your " nor "you have". Enumerating the ways English
    # can claim possession is a losing game, and the prompt already forbids
    # addressing the reader — "a festival usually calls for X" needs no "you",
    # so rejecting the whole pronoun costs nothing legitimate.
    if _SECOND_PERSON_RE.search(advice):
        return None
    return advice


# Same tolerance as `intent_llm.parse_answer`, and for the same measured
# reason: the models cheap enough to run this are reasoning models that treat
# `strict` as a suggestion. A sentence is easier to recover than a record —
# there is only one field — but the failure mode is identical, and advice
# discarded over a stray brace is the dead-end reply all over again.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_ADVICE_RE = re.compile(r'"advice"\s*:\s*"((?:[^"\\]|\\.)*)"')


def parse_advice(content: str | None) -> str | None:
    """Read the advice, well-formed or not. None if there is nothing usable.

    A clean parse is tried first, so nothing about a compliant answer changes.
    `validate` still applies to whatever is recovered — the ownership check is
    the reason this function exists at all rather than a bare regex, since a
    salvaged "wear your kurta" must be dropped exactly like a clean one.
    """
    if not isinstance(content, str) or not content.strip():
        return None

    text = _FENCE_RE.sub("", content.strip())
    try:
        return validate(json.loads(text))
    except (ValueError, TypeError):
        pass

    found = _ADVICE_RE.search(text)
    if found is None:
        return None
    try:
        advice = json.loads(f'"{found.group(1)}"')
    except ValueError:
        return None
    return validate({"advice": advice})
