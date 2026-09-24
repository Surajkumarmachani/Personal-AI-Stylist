"""The database's enum labels must equal taxonomy.yaml, exactly and in order.

This is the test that keeps taxonomy.yaml as the single source of truth. Without
it, someone hand-edits a migration, the YAML and the schema drift, and the VLM
extraction schema (also generated from the YAML) starts producing values the
database rejects.
"""

from __future__ import annotations

import pathlib

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


ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_every_occasion_is_reachable_and_askable() -> None:
    """A taxonomy occasion nobody can pick or type does not exist to the user.

    Twelve of eighteen had tiles. `office_formal`, `client_meeting` and `wfh`
    cover most of the working week and none were on the screen, while the
    resolver could dress someone for a `funeral` they had no way to choose.

    Both directions are checked, because they fail differently: a missing tile
    is invisible, and a tile whose `ask` phrase resolves somewhere else is
    WORSE -- it silently serves a different occasion than the one tapped.
    """
    import re

    from stylist_domain.intent import LEXICON
    from stylist_domain.taxonomy import load_taxonomy

    ids = {o["id"] for o in load_taxonomy().raw["occasions"]}
    src = (ROOT / "web" / "app" / "OCCASIONS.ts").read_text()
    tiles = re.findall(r'id: "([a-z_]+)", ask: "([^"]+)"', src)

    assert {t for t, _ in tiles} == ids, "every occasion needs exactly one tile"
    for occasion, ask in tiles:
        assert LEXICON.get(ask.lower()) == occasion, (
            f"tile {occasion!r} asks {ask!r}, which the lexicon resolves to "
            f"{LEXICON.get(ask.lower())!r}"
        )
    for occasion in ids:
        assert occasion in set(LEXICON.values()), f"{occasion} cannot be typed"
