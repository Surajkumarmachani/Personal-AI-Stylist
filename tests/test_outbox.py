"""Transactional outbox (§C1).

THE TEST THAT JUSTIFIES THE WHOLE PATTERN is
`test_crash_before_commit_leaves_nothing`. If a crash between "row written" and
"job enqueued" can leave a garment with no processing, the product silently
loses user uploads and no retry logic can recover them, because the intent was
never recorded. These tests are the proof that it cannot happen.

The crash is simulated two ways, on purpose:
  - a rolled-back transaction (deterministic, runs everywhere)
  - a real `kill -9` of a subprocess mid-transaction (proves the database, not
    our code, is what guarantees this)
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from stylist_db.outbox import emit, unsent_depth

pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parents[1]


async def _seed_user(session, user_id: uuid.UUID) -> None:
    await session.execute(
        text("INSERT INTO users (id, email, password_hash) VALUES (:id, :email, 'x')"),
        {"id": user_id, "email": f"outbox-{user_id}@example.com"},
    )


async def test_emit_refuses_to_run_outside_a_transaction(app_sessionmaker) -> None:
    """The guard that makes the pattern hard to misuse.

    An emit() that quietly opened its own transaction would commit the event
    independently of the state change — reintroducing exactly the split-write
    bug the outbox removes, while looking correct at the call site.
    """
    async with app_sessionmaker() as session:
        with pytest.raises(RuntimeError, match="outside a transaction"):
            await emit(
                session,
                aggregate_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                event_type="garment.ingested",
                payload={},
            )


async def test_row_and_event_commit_together(owner_engine) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    user_id, garment_id = uuid.uuid4(), uuid.uuid4()

    async with maker() as session, session.begin():
        await _seed_user(session, user_id)
        await session.execute(
            text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(user_id)}
        )
        await session.execute(
            text(
                "INSERT INTO garments (id, user_id, original_key, state) "
                "VALUES (:id, :uid, 'k', 'received')"
            ),
            {"id": garment_id, "uid": user_id},
        )
        await emit(
            session,
            aggregate_id=garment_id,
            user_id=user_id,
            event_type="garment.ingested",
            payload={"key": "k"},
        )

    async with maker() as session:
        garments = await session.execute(
            text("SELECT count(*) FROM garments WHERE id = :id"), {"id": garment_id}
        )
        events = await session.execute(
            text("SELECT count(*) FROM outbox WHERE aggregate_id = :id"), {"id": garment_id}
        )
        assert garments.scalar_one() == 1
        assert events.scalar_one() == 1

    async with maker() as session, session.begin():
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        await session.execute(
            text("DELETE FROM outbox WHERE aggregate_id = :id"), {"id": garment_id}
        )


async def test_crash_before_commit_leaves_nothing(owner_engine) -> None:
    """Rollback stands in for the crash: same observable outcome, deterministic.

    Assert BOTH are absent. A garment with no event would be an upload that is
    never processed; an event with no garment would be a job pointing at a row
    that does not exist.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    user_id, garment_id = uuid.uuid4(), uuid.uuid4()

    async with maker() as session, session.begin():
        await _seed_user(session, user_id)

    session = maker()
    await session.begin()
    await session.execute(
        text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(user_id)}
    )
    await session.execute(
        text(
            "INSERT INTO garments (id, user_id, original_key, state) "
            "VALUES (:id, :uid, 'k', 'received')"
        ),
        {"id": garment_id, "uid": user_id},
    )
    await emit(
        session,
        aggregate_id=garment_id,
        user_id=user_id,
        event_type="garment.ingested",
        payload={"key": "k"},
    )
    await session.flush()  # the writes have reached the server, uncommitted
    await session.rollback()  # <- the crash
    await session.close()

    async with maker() as verify:
        garments = await verify.execute(
            text("SELECT count(*) FROM garments WHERE id = :id"), {"id": garment_id}
        )
        events = await verify.execute(
            text("SELECT count(*) FROM outbox WHERE aggregate_id = :id"), {"id": garment_id}
        )
        assert garments.scalar_one() == 0, "orphan garment survived the crash"
        assert events.scalar_one() == 0, "orphan outbox event survived the crash"

    async with maker() as session, session.begin():
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})


async def test_real_sigkill_before_commit_leaves_nothing(owner_engine) -> None:
    """The same property, proven against a process that is actually killed.

    The rollback test above exercises our code path; this one proves the
    guarantee comes from Postgres. A SIGKILLed process cannot run cleanup, so
    if anything survives here, the durability claim is false.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    user_id, garment_id = uuid.uuid4(), uuid.uuid4()

    async with maker() as session, session.begin():
        await _seed_user(session, user_id)

    dsn = os.environ["MIGRATION_DATABASE_URL"]
    script = textwrap.dedent(
        f"""
        import asyncio, os, sys
        sys.path[:0] = ["packages", "services"]
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        async def main():
            dsn = {dsn!r}.replace("postgresql://", "postgresql+asyncpg://", 1)
            engine = create_async_engine(dsn)
            async with engine.connect() as conn:
                trans = await conn.begin()
                await conn.execute(
                    text("SELECT set_config('app.user_id', :uid, true)"),
                    {{"uid": {str(user_id)!r}}},
                )
                await conn.execute(
                    text(
                        "INSERT INTO garments (id, user_id, original_key, state) "
                        "VALUES (:id, :uid, 'k', 'received')"
                    ),
                    {{"id": {str(garment_id)!r}, "uid": {str(user_id)!r}}},
                )
                await conn.execute(
                    text(
                        "INSERT INTO outbox (id, aggregate_id, user_id, event_type, payload) "
                        "VALUES (gen_random_uuid(), :agg, :uid, 'garment.ingested', '{{}}')"
                    ),
                    {{"agg": {str(garment_id)!r}, "uid": {str(user_id)!r}}},
                )
                print("UNCOMMITTED", flush=True)
                await asyncio.sleep(60)

        asyncio.run(main())
        """
    )

    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline().strip()
        if line != "UNCOMMITTED":
            proc.kill()
            stderr = proc.stderr.read() if proc.stderr else ""
            pytest.skip(f"child could not reach the uncommitted state: {line} {stderr[:300]}")
        os.kill(proc.pid, signal.SIGKILL)  # no cleanup possible
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()

    async with maker() as verify:
        garments = await verify.execute(
            text("SELECT count(*) FROM garments WHERE id = :id"), {"id": garment_id}
        )
        events = await verify.execute(
            text("SELECT count(*) FROM outbox WHERE aggregate_id = :id"), {"id": garment_id}
        )
        assert garments.scalar_one() == 0, "SIGKILL left an orphan garment"
        assert events.scalar_one() == 0, "SIGKILL left an orphan outbox event"

    async with maker() as session, session.begin():
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})


async def test_unsent_depth_is_observable(owner_engine) -> None:
    """Relay lag has to be measurable or a wedged relay is invisible: ingests
    simply stop starting, and nothing errors."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    user_id, garment_id = uuid.uuid4(), uuid.uuid4()

    async with maker() as session, session.begin():
        await _seed_user(session, user_id)
        before = await unsent_depth(session)
        await emit(
            session,
            aggregate_id=garment_id,
            user_id=user_id,
            event_type="garment.ingested",
            payload={},
        )

    async with maker() as session, session.begin():
        after = await unsent_depth(session)
        assert after == before + 1

        await session.execute(
            text("DELETE FROM outbox WHERE aggregate_id = :id"), {"id": garment_id}
        )
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
