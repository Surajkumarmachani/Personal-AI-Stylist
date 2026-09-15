"""Phase 8 — `outfit_feedback` is append-only, and the database enforces it.

WHY THIS FILE EXISTS
--------------------
The first version of migration 0009 said `GRANT SELECT, INSERT ON
outfit_feedback TO stylist_app` and believed that made the table append-only.
It did not. Migration 0001 sets

    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO stylist_app

so every table created afterwards arrives with the full set already attached.
A narrower GRANT adds nothing and removes nothing — it READS like a restriction
and is a no-op. `UPDATE outfit_feedback` succeeded as `stylist_app` with that
grant in place.

This matters more than a tidiness issue. The style vector, the wear-through
rate and everything Phase 11 will learn from are REPLAYS of this table. One
silent UPDATE makes `rebuild_style_vectors.py` disagree with live state for a
reason nobody can reconstruct afterwards, because the evidence of the change is
the thing that got overwritten.

So the guarantee is asserted against a real Postgres, in the direction that
fails — the same standard `tests/test_rls_isolation.py` holds RLS to.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker


async def _seed_one(owner_engine, user_id: uuid.UUID) -> uuid.UUID:
    """One feedback row, written as the OWNER so the test is about privileges
    rather than about whether the insert path works."""
    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    event_id = uuid.uuid4()
    async with maker() as session, session.begin():
        await session.execute(
            text("INSERT INTO users (id, email, password_hash) VALUES (:id, :e, 'x')"),
            {"id": user_id, "e": f"fb-{user_id}@example.com"},
        )
        await session.execute(
            text(
                "INSERT INTO outfit_feedback "
                "(id, user_id, garment_ids, garment_set_hash, kind) "
                "VALUES (:id, :uid, CAST(:ids AS uuid[]), :h, 'like')"
            ),
            {"id": event_id, "uid": user_id, "ids": [str(uuid.uuid4())], "h": "h" * 64},
        )
    return event_id


async def _cleanup(owner_engine, user_id: uuid.UUID) -> None:
    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})


async def test_the_app_role_cannot_update_feedback(app_sessionmaker, owner_engine) -> None:
    """A narrower GRANT did not do this. Only a REVOKE does."""
    user_id = uuid.uuid4()
    await _seed_one(owner_engine, user_id)
    try:
        async with app_sessionmaker() as session:
            await session.execute(
                text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(user_id)}
            )
            with pytest.raises(ProgrammingError, match="permission denied"):
                await session.execute(text("UPDATE outfit_feedback SET kind = 'dislike'"))
    finally:
        await _cleanup(owner_engine, user_id)


async def test_the_app_role_cannot_delete_feedback(app_sessionmaker, owner_engine) -> None:
    """Deleting the evidence is worse than changing it: a replay cannot even
    detect that something is missing."""
    user_id = uuid.uuid4()
    await _seed_one(owner_engine, user_id)
    try:
        async with app_sessionmaker() as session:
            await session.execute(
                text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(user_id)}
            )
            with pytest.raises(ProgrammingError, match="permission denied"):
                await session.execute(text("DELETE FROM outfit_feedback"))
    finally:
        await _cleanup(owner_engine, user_id)


async def test_the_app_role_can_still_read_and_append(app_sessionmaker, owner_engine) -> None:
    """The counterweight. A permission set tightened until the feature stops
    working is not a safety property, it is an outage."""
    user_id = uuid.uuid4()
    await _seed_one(owner_engine, user_id)
    try:
        async with app_sessionmaker() as session:
            await session.execute(
                text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(user_id)}
            )
            await session.execute(
                text(
                    "INSERT INTO outfit_feedback "
                    "(id, user_id, garment_ids, garment_set_hash, kind) "
                    "VALUES (:id, :uid, CAST(:ids AS uuid[]), :h, 'worn')"
                ),
                {
                    "id": uuid.uuid4(),
                    "uid": user_id,
                    "ids": [str(uuid.uuid4())],
                    "h": "g" * 64,
                },
            )
            rows = await session.execute(text("SELECT count(*) FROM outfit_feedback"))
            assert rows.scalar() == 2
            await session.commit()
    finally:
        await _cleanup(owner_engine, user_id)


async def test_the_privilege_set_is_exactly_select_and_insert(owner_engine) -> None:
    """Asserted directly, so a future migration that re-grants UPDATE — or a
    new `ALTER DEFAULT PRIVILEGES` — fails here rather than being discovered
    when a replay disagrees with live state months later."""
    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with maker() as session:
        rows = await session.execute(
            text(
                "SELECT privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = 'stylist_app' AND table_name = 'outfit_feedback'"
            )
        )
        assert {r[0] for r in rows} == {"SELECT", "INSERT"}
