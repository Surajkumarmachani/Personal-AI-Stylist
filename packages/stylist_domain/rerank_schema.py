"""The reranker's prompt and response schema (Step 7.1). Pure.

NO IMAGES, AND THAT IS THE DESIGN
---------------------------------
The reranker sees structured JSON — slot, subcategory, colour, material,
formality, warmth — and never a photograph. Three reasons, in order of how much
they matter:

  1. LATENCY. §B1 gives `/suggestions` 1500ms. A vision call on eight outfits
     cannot be done in 1200ms, so images would mean missing the SLO on every
     request rather than degrading on some.
  2. COST. ~1.2k in / 300 out of text is a rounding error next to eight grids
     of pixels, on a path hit every morning by every user.
  3. PRIVACY. Tagging already sends pixels once, at ingest, behind a moderation
     gate. Sending them again on every suggestion multiplies that exposure for
     a task that does not need them — the tags ARE the extraction.

WHAT THE MODEL IS ASKED TO DO, AND WHAT IT CANNOT DO
----------------------------------------------------
It reorders a list and writes a caption. It cannot add a garment, invent one,
or assemble a new combination: the validator (§C3) checks output ids against
input ids as an exact set, so those failures are structurally impossible rather
than prompt-dependent. This prompt therefore does not beg the model to behave —
it explains the task, and the gate does the enforcing.
"""

from __future__ import annotations

import json
from typing import Any

# §C3 assertion 5 caps the rationale at 40 words. Asking for ~25 leaves the
# model room to be under the limit without being clipped: a prompt that asks
# for exactly the maximum produces a steady trickle of 41-word rejections.
TARGET_RATIONALE_WORDS = 25

# Top-8 from the deterministic scorer (Step 7.1). Not top-20: the reranker is
# there to fix the ORDER of plausible outfits, and an outfit the scorer ranked
# 19th is not one the model should be promoting to first — if it should, the
# scorer is wrong and that is the thing to fix.
RERANK_TOP_N = 8


def build_schema() -> dict[str, Any]:
    """Strict JSON schema for the rerank response.

    `garment_ids` is a plain string array rather than an enum of the ids we
    sent. It is tempting to encode the id set here and let the decoder enforce
    assertion 2 for free, but that would make the guarantee depend on the
    provider honouring `strict` — and the guarantee that invented clothing is
    impossible must not be something a provider can opt out of. The schema is
    the polite request; the validator is the gate.
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "outfit_rerank",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "outfits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "garment_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        "The garment ids of one outfit, copied "
                                        "EXACTLY from the input. Never invent one."
                                    ),
                                },
                                "rationale": {
                                    "type": "string",
                                    "description": (
                                        f"At most {TARGET_RATIONALE_WORDS} words on why this "
                                        "outfit suits the occasion and weather. Describe the "
                                        "CLOTHES, never the wearer's body. No prices, no links."
                                    ),
                                },
                                "confidence": {
                                    "type": "number",
                                    "minimum": 0,
                                    "maximum": 1,
                                    "description": (
                                        "How sure you are this is a good outfit for the "
                                        "context. Below 0.5 it is ranked under the "
                                        "deterministic order, so be honest rather than safe."
                                    ),
                                },
                            },
                            "required": ["garment_ids", "rationale", "confidence"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["outfits"],
                "additionalProperties": False,
            },
        },
    }


def build_prompt(outfits: list[dict[str, Any]], *, context: dict[str, Any]) -> str:
    """The instruction text.

    `outfits` is already-serialisable: each is `{"garment_ids": [...],
    "items": [{slot, subcategory, colour, material, formality, warmth}, ...]}`.
    Building that projection is the CALLER's job, because deciding which
    garment fields leave the building is a privacy decision and it should be
    visible at the call site rather than buried in a prompt builder.
    """
    return (
        "You are ordering outfits a user could wear today, chosen from clothes "
        "they already own.\n\n"
        f"Context: {json.dumps(context, sort_keys=True)}\n\n"
        f"Outfits (already filtered for weather, occasion and slot rules):\n"
        f"{json.dumps(outfits, sort_keys=True, indent=1)}\n\n"
        "Return every outfit, best first. For each one:\n"
        f"  - copy `garment_ids` EXACTLY as given; never invent or substitute an id\n"
        f"  - write at most {TARGET_RATIONALE_WORDS} words on why it suits the context\n"
        "  - describe the clothes, not the wearer's body; no prices, no links\n"
        "  - give an honest confidence; a low one demotes the outfit rather "
        "than discarding your answer\n\n"
        "These outfits are all wearable — you are judging which is BEST for "
        "this occasion and weather, not whether they are valid."
    )
