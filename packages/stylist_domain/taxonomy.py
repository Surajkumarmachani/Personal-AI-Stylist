"""Taxonomy loader — the single entry point to config/taxonomy.yaml.

`packages/stylist_domain` must have ZERO imports from db, api, or clients.
It is pure functions over plain data, so the scorer and slot rules stay
unit-testable without a database. Do not add an import that breaks that;
`tests/test_domain_purity.py` enforces it.

Everything downstream — Postgres enum types, VLM extraction schemas, slot
legality rules, the ATR->slot mapping, golden-set label validation — derives
from this loader. Never hand-copy a value out of the YAML.
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Fields that become Postgres ENUM types. The tuple is
# (pg_type_name, yaml_accessor_key) and the ORDER here is the order the
# labels are created in, which is also their sort order in Postgres.
# Adding a value to any of these is a migration (ALTER TYPE ... ADD VALUE).
ENUM_FIELDS: tuple[str, ...] = (
    "slot",
    "subcategory",
    "colour",
    "material",
    "pattern",
    "fit",
    "dress_code",
    "climate_band",
    "occasion",
)


def taxonomy_path() -> Path:
    """Resolve config/taxonomy.yaml.

    Overridable via TAXONOMY_PATH so tests and the Alembic migration can point
    at a fixture without changing import behaviour.
    """
    env = os.environ.get("TAXONOMY_PATH")
    if env:
        return Path(env)
    # packages/stylist_domain/taxonomy.py -> repo root
    return Path(__file__).resolve().parents[2] / "config" / "taxonomy.yaml"


@functools.lru_cache(maxsize=1)
def load_raw(path: str | None = None) -> dict[str, Any]:
    """Parse the YAML once per process. Cached: this is read on every job."""
    p = Path(path) if path else taxonomy_path()
    with p.open() as fh:
        loaded = yaml.safe_load(fh)
    if not isinstance(loaded, dict):
        raise TypeError(f"{p} did not parse to a mapping (got {type(loaded).__name__})")
    return loaded


@dataclass(frozen=True, slots=True)
class Taxonomy:
    """Typed, validated view over taxonomy.yaml.

    Frozen so a caller cannot mutate shared state; every collection is
    converted to an immutable tuple/frozenset at construction.
    """

    version: str
    slots: tuple[str, ...]
    subcategories: tuple[str, ...]
    subcategories_by_slot: dict[str, tuple[str, ...]]
    colours: tuple[str, ...]
    colour_hex: dict[str, str | None]
    colour_family: dict[str, str]
    materials: tuple[str, ...]
    patterns: tuple[str, ...]
    bold_patterns: frozenset[str]
    fits: tuple[str, ...]
    dress_codes: tuple[str, ...]
    dress_code_compatibility: dict[str, tuple[str, ...]]
    dress_code_formality_range: dict[str, tuple[int, int]]
    climate_bands: tuple[str, ...]
    occasions: tuple[str, ...]
    atr_to_slot: dict[str, str | None]
    slot_hint_confidence: float
    fields: dict[str, dict[str, Any]]
    eval_floors: dict[str, Any]
    raw: dict[str, Any]

    # ---- enum access, used by the migration generator -------------------

    def enum_values(self, field: str) -> tuple[str, ...]:
        """Labels for a Postgres ENUM type, in creation order."""
        match field:
            case "slot":
                return self.slots
            case "subcategory":
                return self.subcategories
            case "colour":
                return self.colours
            case "material":
                return self.materials
            case "pattern":
                return self.patterns
            case "fit":
                return self.fits
            case "dress_code":
                return self.dress_codes
            case "climate_band":
                return self.climate_bands
            case "occasion":
                return self.occasions
            case _:
                raise KeyError(f"{field} is not an enum field; known: {ENUM_FIELDS}")

    # ---- lookups used by ingest and scoring -----------------------------

    def default_slot_for(self, subcategory: str) -> str:
        for slot, subs in self.subcategories_by_slot.items():
            if subcategory in subs:
                return slot
        raise KeyError(f"unknown subcategory: {subcategory}")

    def slot_for_atr_class(self, atr_class: str) -> str | None:
        """Mask-derived slot. A HINT, not truth — the VLM may override it and
        a user correction always wins. See slot_hint_confidence."""
        return self.atr_to_slot.get(atr_class)

    def dress_codes_compatible(self, a: str, b: str) -> bool:
        return b in self.dress_code_compatibility.get(a, ())


@functools.lru_cache(maxsize=1)
def load_taxonomy(path: str | None = None) -> Taxonomy:
    t = load_raw(path)

    subs_by_slot = {slot: tuple(vals) for slot, vals in t["subcategories"].items()}
    flat_subs = tuple(s for vals in subs_by_slot.values() for s in vals)

    return Taxonomy(
        version=t["version"],
        slots=tuple(s["id"] for s in t["slots"]),
        subcategories=flat_subs,
        subcategories_by_slot=subs_by_slot,
        colours=tuple(c["id"] for c in t["colours"]),
        colour_hex={c["id"]: c.get("hex") for c in t["colours"]},
        colour_family={c["id"]: c["family"] for c in t["colours"]},
        materials=tuple(t["materials"]),
        patterns=tuple(p["id"] for p in t["patterns"]),
        bold_patterns=frozenset(p["id"] for p in t["patterns"] if p["bold"]),
        fits=tuple(t["fits"]),
        dress_codes=tuple(d["id"] for d in t["dress_codes"]),
        dress_code_compatibility={k: tuple(v) for k, v in t["dress_code_compatibility"].items()},
        dress_code_formality_range={
            d["id"]: (d["formality_range"][0], d["formality_range"][1]) for d in t["dress_codes"]
        },
        climate_bands=tuple(b["id"] for b in t["climate_bands"]),
        occasions=tuple(o["id"] for o in t["occasions"]),
        atr_to_slot=dict(t["atr_to_slot"]),
        slot_hint_confidence=float(t["slot_hint_confidence"]),
        fields=dict(t["fields"]),
        eval_floors=dict(t["eval_floors"]),
        raw=t,
    )
