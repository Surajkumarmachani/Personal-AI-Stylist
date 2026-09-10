"""Test fixtures.

Tests run against a REAL Postgres, never SQLite or a mock. RLS, enum types and
`set_config` are Postgres behaviours — testing them against anything else would
verify nothing. Two DSNs are needed and they are not interchangeable:

  MIGRATION_DATABASE_URL  owner/superuser. Creates types, tables, policies.
  DATABASE_URL            the app role: NOSUPERUSER, NOBYPASSRLS.

The distinction IS the test. A superuser bypasses RLS entirely, so a suite that
connects as one would pass while production leaked.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[1]

MIGRATION_DSN = os.environ.get("MIGRATION_DATABASE_URL", "postgresql://localhost:5432/stylist_test")
APP_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://stylist_app:stylist_app_local_only@localhost:5432/stylist_test",
)


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """Run Alembic once per session against the owner DSN."""
    env = {**os.environ, "MIGRATION_DATABASE_URL": MIGRATION_DSN}
    result = subprocess.run(
        ["alembic", "-c", "packages/stylist_db/alembic.ini", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade failed:\n{result.stdout}\n{result.stderr}")


@pytest_asyncio.fixture(autouse=True)
async def initialised_engine(migrated_database):
    """Initialise the process-wide engine used by session helpers.

    The worker's state machine and the SSE stream reach for
    stylist_db.session.tenant_session(), which needs init_engine() to have run.
    In production the service lifespan does it; in tests this fixture stands in
    for that, connected as the LEAST-PRIVILEGE app role so RLS applies to the
    worker code paths exactly as it does in production.
    """
    from stylist_db.session import dispose_engine, init_engine

    init_engine(APP_DSN, pool_size=5)
    yield
    await dispose_engine()


@pytest_asyncio.fixture
async def app_engine():
    """Engine connected as the LEAST-PRIVILEGE app role. RLS applies here."""
    engine = create_async_engine(APP_DSN, poolclass=None)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app_sessionmaker(app_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(app_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def owner_engine():
    """Engine connected as the owner. Used ONLY to seed and to clean up —
    never to assert isolation, because the owner is not the thing under test."""
    engine = create_async_engine(MIGRATION_DSN.replace("postgresql://", "postgresql+asyncpg://"))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def two_tenants(owner_engine) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """Create tenant A with 3 garments and tenant B with 2, then clean up.

    Seeded as the owner with RLS FORCE on, so each INSERT sets the tenant
    context explicitly — which also proves WITH CHECK accepts a matching row.
    """
    a, b = uuid.uuid4(), uuid.uuid4()
    maker = async_sessionmaker(owner_engine, expire_on_commit=False)

    async with maker() as session, session.begin():
        for tenant, email in ((a, f"a-{a}@test.local"), (b, f"b-{b}@test.local")):
            await session.execute(
                text("INSERT INTO users (id, email, password_hash) VALUES (:id, :email, 'x')"),
                {"id": tenant, "email": email},
            )
        for tenant, count in ((a, 3), (b, 2)):
            await session.execute(
                text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(tenant)}
            )
            for i in range(count):
                await session.execute(
                    text(
                        "INSERT INTO garments (id, user_id, original_key, state) "
                        "VALUES (:id, :uid, :key, 'received')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "uid": tenant,
                        "key": f"originals/{tenant}/{i}",
                    },
                )

    yield a, b

    async with maker() as session, session.begin():
        # FK cascade removes garments/jobs/profile with the user.
        await session.execute(text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": [a, b]})
