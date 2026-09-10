"""The database's enum labels must equal taxonomy.yaml, exactly and in order.

This is the test that keeps taxonomy.yaml as the single source of truth. Without
it, someone hand-edits a migration, the YAML and the schema drift, and the VLM
extraction schema (also generated from the YAML) starts producing values the
database rejects.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from stylist_db.taxonomy_enums import expected_labels

pytestmark = pytest.mark.asyncio


async def test_db_enums_match_taxonomy_yaml(app_sessionmaker) -> None:
    expected = expected_labels()

    async with app_sessionmaker() as session:
        rows = await session.execute(
            text(
                """
                SELECT t.typname, e.enumlabel
                FROM pg_type t
                JOIN pg_enum e ON e.enumtypid = t.oid
                WHERE t.typname = ANY(:names)
                ORDER BY t.typname, e.enumsortorder
                """
            ),
            {"names": list(expected)},
        )
        actual: dict[str, list[str]] = {}
        for typname, label in rows:
            actual.setdefault(typname, []).append(label)

    missing_types = set(expected) - set(actual)
    assert not missing_types, f"enum types absent from the database: {missing_types}"

    for type_name, want in expected.items():
        got = tuple(actual[type_name])
        assert got == want, (
            f"enum {type_name} drifted from taxonomy.yaml\n"
            f"  in yaml, not in db: {set(want) - set(got)}\n"
            f"  in db, not in yaml: {set(got) - set(want)}"
        )


async def test_no_other_value_anywhere(app_sessionmaker) -> None:
    """taxonomy.yaml forbids an `other` escape hatch by design; assert the
    database agrees, in case someone adds one with ALTER TYPE directly."""
    async with app_sessionmaker() as session:
        rows = await session.execute(
            text(
                "SELECT t.typname, e.enumlabel FROM pg_type t "
                "JOIN pg_enum e ON e.enumtypid = t.oid "
                "WHERE e.enumlabel IN ('other', 'unknown_other', 'misc')"
            )
        )
        offenders = list(rows)
    assert not offenders, f"forbidden escape-hatch enum values present: {offenders}"
