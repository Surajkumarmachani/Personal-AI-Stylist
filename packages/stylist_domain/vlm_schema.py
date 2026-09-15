"""VLM extraction schema, generated from taxonomy.yaml (Step 4.2).

THE POINT: A MODEL RETURNING A BAD VALUE IS A SCHEMA VIOLATION, NOT A BAD ROW
-----------------------------------------------------------------------------
Every enum in this schema comes from the same loader that generates the
Postgres types. So if the model answers `subcategory: "crimson blouse"`, it
fails validation mechanically before it can touch a row — rather than being
written, accepted, and later discovered as a value no filter matches.

Hand-listing the enums here instead would create a third copy of the taxonomy
(YAML, Postgres, prompt) and they would drift. When they drift, the model gets
told about values the database will reject, which presents as a mysterious
insert failure on a response that looked fine.

WHAT IS ASKED AND WHAT IS NOT
-----------------------------
Only `tier: vlm` fields are requested. `slot`, `primary_colour`,
`secondary_colour` and `pattern` are `tier: local` — already answered for free
by segmentation and CIELAB k-means — and `climate_bands` is `tier: rule`,
derived. Asking the model for any of them would pay tokens for an answer we
already have, and give it the chance to contradict a cheaper, more reliable
source.
"""

from __future__ import annotations

from typing import Any

from stylist_domain.taxonomy import Taxonomy

# Fields the VLM is asked for. Derived from taxonomy.yaml's `tier: vlm` marking
# rather than hardcoded — `vlm_fields()` below asserts the two agree, so moving
# a field between tiers in the YAML is picked up here.
_EXPECTED_VLM_FIELDS = ("subcategory", "material", "formality", "dress_code", "warmth", "fit")


def vlm_fields(taxonomy: Taxonomy) -> tuple[str, ...]:
    """The `tier: vlm` fields, in a stable order.

    Cross-checked against the expected list so that re-tiering a field in
    taxonomy.yaml surfaces here instead of silently changing what we pay for.
    """
    declared = tuple(name for name, cfg in taxonomy.fields.items() if cfg.get("tier") == "vlm")
    unexpected = set(declared) - set(_EXPECTED_VLM_FIELDS)
    missing = set(_EXPECTED_VLM_FIELDS) - set(declared)
    if unexpected or missing:
        raise ValueError(
            "taxonomy.yaml's tier=vlm fields disagree with the VLM schema: "
            f"unexpected={sorted(unexpected)} missing={sorted(missing)}. "
            "Re-tiering a field changes what the model is asked for and what "
            "it costs; update _EXPECTED_VLM_FIELDS deliberately."
        )
    return tuple(f for f in _EXPECTED_VLM_FIELDS if f in declared)


def required_fields(taxonomy: Taxonomy) -> tuple[str, ...]:
    """VLM fields the taxonomy marks `required: true`.

    A missing optional field is acceptable (material from a photo is genuinely
    hard); a missing required one is a response we cannot use.
    """
    return tuple(
        name for name in vlm_fields(taxonomy) if taxonomy.fields[name].get("required") is True
    )


# Largest enum we will inline in the response schema.
#
# Gemini's responseSchema rejects the full 144-value `subcategory` list with a
# bare `400 INVALID_ARGUMENT` that names no field and no reason — the least
# actionable error in this pipeline. Measured against gemini-3.6-flash by
# bisection: 128 values pass, 136 fail. This sits at the last passing value.
#
# OpenAI and Anthropic both accept the full list, so this is a lowest common
# denominator rather than a universal limit. ONE schema shape is sent to every
# provider anyway: a schema that varies per provider means the fallback in
# litellm/config.yaml exercises a shape that was never tested, which is exactly
# when you do not want a surprise.
#
# Dropping an enum does NOT weaken validation. The permitted values move into
# the prompt (build_prompt lists them), and `_parse` in the tag stage still
# checks every value against the taxonomy and drops unknown ones per field.
# The schema was always the polite request; the parse is the actual gate.
MAX_SCHEMA_ENUM = 128


def _enum_values(taxonomy: Taxonomy) -> dict[str, list[str]]:
    """Permitted values per enum field, from the one taxonomy loader."""
    return {
        "subcategory": list(taxonomy.subcategories),
        "material": list(taxonomy.materials),
        "dress_code": list(taxonomy.dress_codes),
        "fit": list(taxonomy.fits),
    }


def prompt_listed_enums(taxonomy: Taxonomy) -> tuple[str, ...]:
    """Fields whose values are too numerous for the schema, so the prompt
    must carry them instead. Shared by build_schema and build_prompt so the
    two cannot disagree about which fields the model was told about."""
    return tuple(
        name for name, values in _enum_values(taxonomy).items() if len(values) > MAX_SCHEMA_ENUM
    )


def build_schema(taxonomy: Taxonomy, *, cells: list[str]) -> dict[str, Any]:
    """A strict JSON schema keyed by grid cell.

    Keyed by CELL rather than positional array, because a model that returns
    five items for a six-cell grid must be unambiguous about which one it
    dropped. With a positional array, one omission silently shifts every
    subsequent garment's tags onto the wrong garment — the worst possible
    failure, because every value is individually plausible.
    """
    values = _enum_values(taxonomy)
    listed_in_prompt = set(prompt_listed_enums(taxonomy))

    def enum_field(name: str) -> dict[str, Any]:
        """An enum property, or a plain string when the list is too long to
        inline. The description names where the values went, so a response
        read in Langfuse is not mystifying."""
        if name in listed_in_prompt:
            return {
                "type": "string",
                "description": (
                    f"One of the {len(values[name])} permitted {name} values "
                    "listed in the prompt. Values outside that list are discarded."
                ),
            }
        return {"type": "string", "enum": values[name]}

    item_properties: dict[str, Any] = {
        "cell": {
            "type": "string",
            "enum": cells,
            "description": "Which labelled cell of the grid this describes.",
        },
        "subcategory": enum_field("subcategory"),
        "material": enum_field("material"),
        "formality": {"type": "integer", "minimum": 1, "maximum": 5},
        "dress_code": enum_field("dress_code"),
        "warmth": {"type": "integer", "minimum": 1, "maximum": 5},
        "fit": enum_field("fit"),
        "confidence": {
            "type": "object",
            # INTEGER PERCENT ON THE WIRE, float 0-1 everywhere else.
            #
            # Asked for as a float, Gemini's constrained decoding degenerates:
            # it emits `0.95000000000...` for thousands of digits until the
            # response hits max_tokens and the JSON is truncated mid-object, so
            # a PERFECTLY GOOD extraction is thrown away by the parser. The
            # number grammar allows unbounded trailing digits and nothing in a
            # float schema can forbid them — `minimum`/`maximum` and
            # `multipleOf` were all measured and none of them helps. An integer
            # cannot express the pathology at all.
            #
            # The tag stage divides by 100 on the way in, so the database, the
            # review thresholds and the API keep the 0-1 floats they always had.
            "description": (
                "Per-field confidence as an INTEGER 0-100 (95 means 0.95). Drives the review gate."
            ),
            "properties": {
                name: {"type": "integer", "minimum": 0, "maximum": 100}
                for name in vlm_fields(taxonomy)
            },
            # EVERY key is required, and the omission of this line was a real
            # bug. `properties` alone constrains the SHAPE of a key that is
            # present and says nothing about whether it must be; Gemini duly
            # returned `subcategory` and `warmth` and dropped the other four
            # while still emitting values for them. The review gate then read a
            # missing confidence as 0.0 and flagged EVERY garment for review —
            # 17 of 17 on the first real run — which makes the review queue the
            # whole wardrobe and destroys the correction-rate signal.
            #
            # OpenAI's strict mode already demands that `required` list every
            # key in `properties`, so this is also what makes the schema
            # portable to the `vlm-tagger-backup` row rather than only valid
            # against the provider that happened to be lenient.
            "required": list(vlm_fields(taxonomy)),
            "additionalProperties": False,
        },
    }

    return {
        "type": "json_schema",
        "json_schema": {
            "name": "garment_tags",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": item_properties,
                            "required": ["cell", *vlm_fields(taxonomy), "confidence"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["items"],
                "additionalProperties": False,
            },
        },
    }


def build_prompt(taxonomy: Taxonomy, *, cells: list[str], hints: dict[str, str]) -> str:
    """The instruction text accompanying the grid.

    `hints` carries what we ALREADY know per cell from local extraction — the
    slot from segmentation, the colour from k-means. Passing them serves two
    purposes: it anchors the model (a garment already known to be in the
    `lower` slot is not a shirt), and it means we never ask for those fields
    back, so a disagreement cannot overwrite a cheaper and more reliable
    answer.
    """
    lines = [
        "You are cataloguing garments for a personal wardrobe app.",
        "",
        "The image is a labelled grid. Each cell contains ONE garment cut out "
        "from a user's photo, on a transparent background.",
        "",
        "For each cell, return the requested attributes using ONLY the "
        "permitted values. Do not invent values. Do not describe cells that "
        "are empty.",
        "",
        "Already determined locally — do NOT contradict or re-report these:",
    ]
    for cell in cells:
        lines.append(f"  {cell}: {hints.get(cell, 'unknown')}")

    lines += [
        "",
        "Notes on the value sets, which are specific to this app:",
        "  - formality is 1-5 AND dress_code is separate. A festive ethnic "
        "outfit and a business suit can both be formality 4; they are not "
        "interchangeable.",
        "  - the subcategory list includes ethnic wear (saree, lehenga_skirt, "
        "kurta, sherwani, anarkali, dupatta...). Use them where they apply "
        "rather than forcing a Western equivalent.",
        "  - warmth is calibrated to a 16-34C range, not a temperate one. A "
        "cotton shirt is 2-3, not 4.",
        "",
        "confidence is an INTEGER from 0 to 100 per field (95 means 95% "
        "certain). Do not write it as a decimal.",
        "",
        "confidence must reflect genuine uncertainty per field. Material from "
        "a photo is often ambiguous; say so with a low number rather than "
        "guessing confidently. Low-confidence fields are shown to the user for "
        "correction, which is a good outcome — a confident wrong answer is not.",
    ]

    # Fields too large to express as a schema enum (see MAX_SCHEMA_ENUM). The
    # schema cannot constrain these, so the prompt is the ONLY place the model
    # learns the vocabulary — omit this and it invents plausible values that
    # the tag stage then drops, which looks like a model that cannot classify
    # clothes rather than a prompt missing its value list.
    for name in prompt_listed_enums(taxonomy):
        permitted = _enum_values(taxonomy)[name]
        lines += [
            "",
            f"Permitted {name} values ({len(permitted)}) — use EXACTLY one of "
            "these strings, verbatim. Anything else is discarded:",
            "  " + ", ".join(permitted),
        ]

    return "\n".join(lines)
