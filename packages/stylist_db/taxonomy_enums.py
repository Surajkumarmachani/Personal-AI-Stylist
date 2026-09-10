"""Generate Postgres ENUM types from config/taxonomy.yaml.

The YAML is the source of truth, forever. Enum labels are NEVER hand-typed
into a migration — they are emitted from here, and `tests/test_taxonomy_enums.py`
asserts that what is in the database matches what is in the YAML.

Adding a value to taxonomy.yaml is a migration:
  1. bump `version` in the YAML
  2. write an Alembic revision calling `add_enum_value()`
  3. bump `extractor_version` on affected garments so the backfill re-tags them

ALTER TYPE ... ADD VALUE cannot run inside a transaction block that later uses
the new value, so a value-adding migration must not also insert rows using it.
Split those into two revisions.
"""

from __future__ import annotations

from stylist_domain.taxonomy import ENUM_FIELDS, load_taxonomy

# Postgres type name for each enum field. `climate_band` is singular here even
# though the garments column is `climate_band[]` — the TYPE is scalar, the
# COLUMN is an array.
PG_TYPE_NAMES: dict[str, str] = {field: field for field in ENUM_FIELDS}


def _quote(label: str) -> str:
    """Single-quote an enum label. Labels come from a checked-in YAML file, but
    quote defensively anyway — this string goes into DDL."""
    if "'" in label:
        raise ValueError(f"enum label must not contain a quote: {label!r}")
    return f"'{label}'"


def create_type_statements(taxonomy_path: str | None = None) -> list[str]:
    """CREATE TYPE ... AS ENUM (...) for every enum field, in ENUM_FIELDS order."""
    tax = load_taxonomy(taxonomy_path)
    stmts: list[str] = []
    for field in ENUM_FIELDS:
        values = tax.enum_values(field)
        if not values:
            raise ValueError(f"enum field {field} has no values in taxonomy.yaml")
        labels = ", ".join(_quote(v) for v in values)
        stmts.append(f"CREATE TYPE {PG_TYPE_NAMES[field]} AS ENUM ({labels})")
    return stmts


def drop_type_statements() -> list[str]:
    """Reverse of the above, for migration downgrade. Reverse order so a type
    another object depends on goes last."""
    return [f"DROP TYPE IF EXISTS {PG_TYPE_NAMES[f]}" for f in reversed(ENUM_FIELDS)]


def add_enum_value(pg_type: str, label: str, *, after: str | None = None) -> str:
    """DDL to add one label to an existing enum type.

    Use IF NOT EXISTS so a re-run is safe. Note Postgres cannot REMOVE an enum
    value — the only way back is recreating the type, so adding a value is a
    one-way door. That is deliberate friction: it is why taxonomy.yaml carries
    the 'no `other` value' rule.
    """
    if pg_type not in PG_TYPE_NAMES.values():
        raise ValueError(f"unknown enum type {pg_type!r}")
    stmt = f"ALTER TYPE {pg_type} ADD VALUE IF NOT EXISTS {_quote(label)}"
    if after:
        stmt += f" AFTER {_quote(after)}"
    return stmt


def expected_labels(taxonomy_path: str | None = None) -> dict[str, tuple[str, ...]]:
    """What the database SHOULD contain, for the CI conformance test."""
    tax = load_taxonomy(taxonomy_path)
    return {PG_TYPE_NAMES[f]: tax.enum_values(f) for f in ENUM_FIELDS}


if __name__ == "__main__":  # pragma: no cover - manual inspection aid
    for statement in create_type_statements():
        print(f"{statement};")
