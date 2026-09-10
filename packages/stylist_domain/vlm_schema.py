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


def build_schema(taxonomy: Taxonomy, *, cells: list[str]) -> dict[str, Any]:
    """A strict JSON schema keyed by grid cell.

    Keyed by CELL rather than positional array, because a model that returns
    five items for a six-cell grid must be unambiguous about which one it
    dropped. With a positional array, one omission silently shifts every
    subsequent garment's tags onto the wrong garment — the worst possible
    failure, because every value is individually plausible.
    """
    item_properties: dict[str, Any] = {
        "cell": {
            "type": "string",
            "enum": cells,
            "description": "Which labelled cell of the grid this describes.",
        },
        "subcategory": {"type": "string", "enum": list(taxonomy.subcategories)},
        "material": {"type": "string", "enum": list(taxonomy.materials)},
        "formality": {"type": "integer", "minimum": 1, "maximum": 5},
        "dress_code": {"type": "string", "enum": list(taxonomy.dress_codes)},
        "warmth": {"type": "integer", "minimum": 1, "maximum": 5},
        "fit": {"type": "string", "enum": list(taxonomy.fits)},
        "confidence": {
            "type": "object",
            "description": "Per-field confidence, 0-1. Drives the review gate.",
            "properties": {
                name: {"type": "number", "minimum": 0, "maximum": 1}
                for name in vlm_fields(taxonomy)
            },
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
        "confidence must reflect genuine uncertainty per field. Material from "
        "a photo is often ambiguous; say so with a low number rather than "
        "guessing confidently. Low-confidence fields are shown to the user for "
        "correction, which is a good outcome — a confident wrong answer is not.",
    ]
    return "\n".join(lines)
