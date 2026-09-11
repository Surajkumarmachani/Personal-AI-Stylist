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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from stylist_clients.storage import ALLOWED_CONTENT_TYPES, PresignedUpload

REPO_ROOT = Path(__file__).resolve().parents[1]

MIGRATION_DSN = os.environ.get("MIGRATION_DATABASE_URL", "postgresql://localhost:5432/stylist_test")
APP_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://stylist_app:stylist_app_local_only@localhost:5432/stylist_test",
)


@pytest.fixture(scope="session", autouse=True)
def release_onnx_sessions():
    """Drop ONNX sessions deterministically before the interpreter exits.

    Left to Python's shutdown, onnxruntime's native session destructors race
    its thread-pool teardown and the process aborts with

        libc++abi: terminating due to uncaught exception of type
        std::__1::system_error: recursive_mutex lock failed

    AFTER every test has passed — pytest reports "106 passed" and exits 134.
    That is the worst possible flake: a red build on a green run, roughly one
    time in two, with nothing in the test output to explain it.

    Releasing the sessions while the interpreter is still healthy avoids the
    race. Ordered before the DB fixture below so it tears down last.
    """
    yield
    import gc

    try:
        from stylist_ml import matting

        matting._session.cache_clear()
    except Exception:
        pass
    gc.collect()


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


# ---------------------------------------------------------------------------
# Shared API fixtures.
#
# These lived in test_api_smoke.py until Phase 5 needed the same authenticated
# client for wear-log and search tests. A fixture defined in a test MODULE is
# not visible to other modules, so the choice was to duplicate the fake object
# store or move it here — and two fakes drifting apart is a worse problem than
# a slightly larger conftest.
# ---------------------------------------------------------------------------


class FakeObjectStore:
    """In-memory stand-in. Mirrors the real contract: presign refuses a
    disallowed content type, and head() returns None until bytes 'exist'."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.presign_ttl_seconds = 900

    def presign_upload(
        self, *, user_id: uuid.UUID | str, content_type: str, max_bytes: int = 12 * 1024 * 1024
    ) -> PresignedUpload:
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise ValueError(f"content_type not allowed: {content_type}")
        upload_id = str(uuid.uuid4())
        key = f"originals/{user_id}/{upload_id}"
        return PresignedUpload(
            upload_id=upload_id,
            key=key,
            url="http://fake-storage.local/stylist-local",
            fields={"key": key, "Content-Type": content_type, "policy": "ZmFrZQ=="},
            expires_at=datetime.now(UTC) + timedelta(seconds=900),
            max_bytes=max_bytes,
        )

    def complete_upload(self, key: str) -> None:
        """Simulate the client's direct-to-storage PUT succeeding."""
        self.objects[key] = {"ContentLength": 2048, "ContentType": "image/jpeg"}

    def head(self, key: str) -> dict[str, Any] | None:
        return self.objects.get(key)

    def presign_download(self, key: str, *, ttl_seconds: int | None = None) -> str:
        return f"http://fake-storage.local/{key}?signed=1"


@pytest_asyncio.fixture
async def api(migrated_database):
    """The real app, with only object storage faked."""
    os.environ.setdefault("REDIS_QUEUE_URL", "redis://localhost:6379/0")
    # No worker runs in these tests, so a job never leaves `received` and the
    # SSE stream would otherwise poll until the production 300s cap — a
    # five-minute test suite. Shrink the cap rather than sleeping around it.
    os.environ["SSE_MAX_STREAM_SECONDS"] = "1.5"
    os.environ["SSE_POLL_INTERVAL_SECONDS"] = "0.1"
    from stylist_api.settings import get_settings

    get_settings.cache_clear()

    from stylist_api.deps import object_store
    from stylist_api.main import create_app

    app = create_app()
    fake = FakeObjectStore()
    app.dependency_overrides[object_store] = lambda: fake

    async with app.router.lifespan_context(app):
        try:
            await app.state.queue_redis.ping()
        except Exception:
            pytest.skip("redis not reachable")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            client.fake_store = fake  # type: ignore[attr-defined]
            yield client


async def _register(client: AsyncClient) -> tuple[str, str]:
    email = f"user-{uuid.uuid4()}@example.com"
    resp = await client.post(
        "/auth/register", json={"email": email, "password": "a-long-enough-password"}
    )
    assert resp.status_code == 201, resp.text
    return email, resp.json()["access_token"]


async def _upload_one(client: AsyncClient, token: str) -> tuple[str, str]:
    """Presign, then simulate the client's upload completing."""
    resp = await client.post(
        "/uploads/presign",
        json={"content_type": "image/jpeg"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    client.fake_store.complete_upload(body["key"])  # type: ignore[attr-defined]
    return body["upload_id"], body["key"]


@pytest_asyncio.fixture
async def registered(api):
    """An authenticated tenant that owns exactly one garment.

    The garment is created through the real ingest endpoint rather than an
    INSERT, so it carries whatever columns and defaults ingest actually writes.
    No worker runs in these tests, so it stays at `received` — which is fine
    for wear, laundry and search, none of which require a cutout.
    """
    email, token = await _register(api)
    auth = {"Authorization": f"Bearer {token}"}
    upload_id, key = await _upload_one(api, token)
    resp = await api.post(
        "/garments/ingest",
        json={"upload_ids": [upload_id], "keys": [key]},
        headers={**auth, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 202, resp.text

    class _Tenant:
        pass

    tenant = _Tenant()
    tenant.email = email  # type: ignore[attr-defined]
    tenant.token = token  # type: ignore[attr-defined]
    tenant.auth = auth  # type: ignore[attr-defined]
    return tenant


@pytest_asyncio.fixture
async def second_tenant(api, registered):
    """A DIFFERENT authenticated tenant, also owning one garment.

    Separate from the `two_tenants` fixture, which yields bare user ids for
    direct-SQL isolation tests; this one is for isolation asserted through the
    API, where the request has to carry a real token.
    """
    email, token = await _register(api)
    auth = {"Authorization": f"Bearer {token}"}
    upload_id, key = await _upload_one(api, token)
    await api.post(
        "/garments/ingest",
        json={"upload_ids": [upload_id], "keys": [key]},
        headers={**auth, "Idempotency-Key": str(uuid.uuid4())},
    )

    class _Tenant:
        pass

    tenant = _Tenant()
    tenant.email = email  # type: ignore[attr-defined]
    tenant.token = token  # type: ignore[attr-defined]
    tenant.auth = auth  # type: ignore[attr-defined]
    return tenant
