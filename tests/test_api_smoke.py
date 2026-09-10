"""End-to-end API tests through the real ASGI app.

Object storage is the one dependency substituted here, with an in-memory fake.
Everything else is real: real Postgres with RLS active as the least-privilege
app role, real Redis, real JWT, the real dependency graph. The point is to
exercise the paths a client actually takes, including the tenant boundary,
rather than calling functions directly.

The load-bearing test is `test_ingest_is_idempotent`: Phase 1's exit criterion
is that POSTing the same Idempotency-Key twice returns the SAME job id. On a
60-photo onboarding burst over mobile data, retries are normal traffic, and a
duplicated ingest is a doubled VLM bill plus duplicate wardrobe rows.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from stylist_clients.storage import ALLOWED_CONTENT_TYPES, PresignedUpload

pytestmark = pytest.mark.asyncio


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


# --------------------------------------------------------------------------


async def test_healthz_does_not_touch_dependencies(api: AsyncClient) -> None:
    """Liveness must not check Postgres. If it did, a brief DB blip would
    restart every pod and turn a degradation into an outage."""
    resp = await api.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readyz_reports_each_dependency(api: AsyncClient) -> None:
    resp = await api.get("/readyz")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ready"] is True
    assert body["checks"]["postgres"] == "ok"


async def test_register_then_authenticated_request(api: AsyncClient) -> None:
    _email, token = await _register(api)
    resp = await api.get("/garments", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json() == []


async def test_unauthenticated_request_is_rejected(api: AsyncClient) -> None:
    assert (await api.get("/garments")).status_code == 401


async def test_refresh_token_cannot_be_used_as_access_token(api: AsyncClient) -> None:
    """Otherwise the 15-minute access lifetime is decorative."""
    email = f"user-{uuid.uuid4()}@example.com"
    resp = await api.post(
        "/auth/register", json={"email": email, "password": "a-long-enough-password"}
    )
    refresh = resp.json()["refresh_token"]
    resp = await api.get("/garments", headers={"Authorization": f"Bearer {refresh}"})
    assert resp.status_code == 401


async def test_duplicate_email_is_rejected(api: AsyncClient) -> None:
    email = f"user-{uuid.uuid4()}@example.com"
    payload = {"email": email, "password": "a-long-enough-password"}
    assert (await api.post("/auth/register", json=payload)).status_code == 201
    assert (await api.post("/auth/register", json=payload)).status_code == 409


async def test_presign_rejects_disallowed_content_type(api: AsyncClient) -> None:
    _email, token = await _register(api)
    resp = await api.post(
        "/uploads/presign",
        json={"content_type": "application/pdf"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


async def test_ingest_is_idempotent(api: AsyncClient) -> None:
    """PHASE 1 EXIT CRITERION: same Idempotency-Key => same job_id."""
    _email, token = await _register(api)
    upload_id, key = await _upload_one(api, token)
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": f"key-{uuid.uuid4()}"}
    body = {"upload_ids": [upload_id], "keys": [key]}

    first = await api.post("/garments/ingest", json=body, headers=headers)
    assert first.status_code == 202, first.text
    first_jobs = first.json()["job_ids"]
    assert len(first_jobs) == 1

    second = await api.post("/garments/ingest", json=body, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["idempotent_replay"] is True
    assert second.json()["job_ids"] == first_jobs, "retry created a different job"

    # And exactly one garment exists, not two.
    listed = await api.get("/garments", headers={"Authorization": f"Bearer {token}"})
    assert len(listed.json()) == 1


async def test_ingest_requires_an_idempotency_key(api: AsyncClient) -> None:
    _email, token = await _register(api)
    upload_id, key = await _upload_one(api, token)
    resp = await api.post(
        "/garments/ingest",
        json={"upload_ids": [upload_id], "keys": [key]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


async def test_ingest_rejects_a_key_from_another_tenant(api: AsyncClient) -> None:
    """Defence in depth. RLS stops the row being readable, but the key check
    stops tenant B enqueueing work over tenant A's photo at all."""
    _email_a, token_a = await _register(api)
    _email_b, token_b = await _register(api)
    upload_id_a, key_a = await _upload_one(api, token_a)

    resp = await api.post(
        "/garments/ingest",
        json={"upload_ids": [upload_id_a], "keys": [key_a]},
        headers={"Authorization": f"Bearer {token_b}", "Idempotency-Key": f"k-{uuid.uuid4()}"},
    )
    assert resp.status_code == 403


async def test_ingest_rejects_a_key_with_no_uploaded_bytes(api: AsyncClient) -> None:
    """A presign handed out is not evidence that an object exists."""
    _email, token = await _register(api)
    resp = await api.post(
        "/uploads/presign",
        json={"content_type": "image/jpeg"},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()  # deliberately NOT completing the upload
    resp = await api.post(
        "/garments/ingest",
        json={"upload_ids": [body["upload_id"]], "keys": [body["key"]]},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": f"k-{uuid.uuid4()}"},
    )
    assert resp.status_code == 409


async def test_wardrobe_is_tenant_scoped_over_http(api: AsyncClient) -> None:
    """The same isolation the RLS unit tests prove, through the full stack."""
    _email_a, token_a = await _register(api)
    _email_b, token_b = await _register(api)

    for _ in range(2):
        upload_id, key = await _upload_one(api, token_a)
        await api.post(
            "/garments/ingest",
            json={"upload_ids": [upload_id], "keys": [key]},
            headers={"Authorization": f"Bearer {token_a}", "Idempotency-Key": f"k-{uuid.uuid4()}"},
        )

    upload_id, key = await _upload_one(api, token_b)
    await api.post(
        "/garments/ingest",
        json={"upload_ids": [upload_id], "keys": [key]},
        headers={"Authorization": f"Bearer {token_b}", "Idempotency-Key": f"k-{uuid.uuid4()}"},
    )

    a_list = await api.get("/garments", headers={"Authorization": f"Bearer {token_a}"})
    b_list = await api.get("/garments", headers={"Authorization": f"Bearer {token_b}"})
    assert len(a_list.json()) == 2
    assert len(b_list.json()) == 1
    a_ids = {g["id"] for g in a_list.json()}
    b_ids = {g["id"] for g in b_list.json()}
    assert not (a_ids & b_ids)


async def test_another_tenants_job_is_404_not_403(api: AsyncClient) -> None:
    """404, because confirming the id exists would itself leak information."""
    _email_a, token_a = await _register(api)
    _email_b, token_b = await _register(api)
    upload_id, key = await _upload_one(api, token_a)
    resp = await api.post(
        "/garments/ingest",
        json={"upload_ids": [upload_id], "keys": [key]},
        headers={"Authorization": f"Bearer {token_a}", "Idempotency-Key": f"k-{uuid.uuid4()}"},
    )
    job_id = resp.json()["job_ids"][0]

    assert (
        await api.get(f"/jobs/{job_id}", headers={"Authorization": f"Bearer {token_a}"})
    ).status_code == 200
    assert (
        await api.get(f"/jobs/{job_id}", headers={"Authorization": f"Bearer {token_b}"})
    ).status_code == 404


async def test_ingest_writes_an_outbox_row_in_the_same_transaction(api: AsyncClient) -> None:
    """The outbox is what makes ingest crash-safe (§C1). No relay exists until
    Phase 2, and that is fine — the intent is durably recorded regardless."""
    from sqlalchemy import text

    from stylist_db.session import system_session

    _email, token = await _register(api)
    upload_id, key = await _upload_one(api, token)
    resp = await api.post(
        "/garments/ingest",
        json={"upload_ids": [upload_id], "keys": [key]},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": f"k-{uuid.uuid4()}"},
    )
    garment_key = key

    async with system_session() as session:
        rows = await session.execute(
            text("SELECT event_type, sent_at FROM outbox WHERE payload->>'key' = :key"),
            {"key": garment_key},
        )
        events = list(rows)

    assert resp.status_code == 202
    assert len(events) == 1
    assert events[0][0] == "garment.ingested"
    assert events[0][1] is None, "nothing should have marked this sent yet"


async def test_job_is_readable_immediately_after_ingest(api: AsyncClient) -> None:
    """Read-your-own-writes, with NO sleep between the write and the read.

    This is the regression test for a race that a sleep would hide. FastAPI runs
    a `yield` dependency's teardown AFTER the response is sent, so when the
    ingest transaction was owned by the `TenantDB` dependency the 202 reached
    the client before the COMMIT — and a client doing the obvious thing (poll
    the job id you were just handed) got a 404. On a fast connection it is
    reliable; on a slow one it looks like a flaky "job not found".

    Any sleep here, however short, makes this test pass against the broken
    code. That is exactly why there isn't one.
    """
    _email, token = await _register(api)
    upload_id, key = await _upload_one(api, token)
    auth = {"Authorization": f"Bearer {token}"}

    resp = await api.post(
        "/garments/ingest",
        json={"upload_ids": [upload_id], "keys": [key]},
        headers={**auth, "Idempotency-Key": f"k-{uuid.uuid4()}"},
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_ids"][0]

    job = await api.get(f"/jobs/{job_id}", headers=auth)
    assert job.status_code == 200, (
        f"the job the API just returned was not readable: {job.status_code} {job.text}"
    )
    assert job.json()["id"] == job_id

    # And the garment, likewise, with no wait.
    listed = await api.get("/garments", headers=auth)
    assert len(listed.json()) == 1


async def test_sse_stream_reports_state_and_closes(api: AsyncClient) -> None:
    """The progress stream emits the current state immediately on connect.

    Emitting on connect (rather than only on the next change) is what makes a
    client that attaches slightly late still correct: it gets the state as it
    is now, not silence until something moves.
    """
    _email, token = await _register(api)
    upload_id, key = await _upload_one(api, token)
    auth = {"Authorization": f"Bearer {token}"}
    resp = await api.post(
        "/garments/ingest",
        json={"upload_ids": [upload_id], "keys": [key]},
        headers={**auth, "Idempotency-Key": f"k-{uuid.uuid4()}"},
    )
    job_id = resp.json()["job_ids"][0]

    # No worker runs in this test, so the job stays at `received` and the
    # stream should report that state and then keep the connection open.
    async with api.stream("GET", f"/jobs/{job_id}/events", headers=auth) as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        async for line in stream.aiter_lines():
            if line.startswith("data:"):
                payload = json.loads(line[5:])
                assert payload["job_id"] == job_id
                assert payload["state"] == "received"
                break


async def test_sse_stream_404s_for_another_tenants_job(api: AsyncClient) -> None:
    """Checked before the stream opens, so it is a clean 404 rather than a 200
    that streams an error the client has to parse."""
    _a, token_a = await _register(api)
    _b, token_b = await _register(api)
    upload_id, key = await _upload_one(api, token_a)
    resp = await api.post(
        "/garments/ingest",
        json={"upload_ids": [upload_id], "keys": [key]},
        headers={
            "Authorization": f"Bearer {token_a}",
            "Idempotency-Key": f"k-{uuid.uuid4()}",
        },
    )
    job_id = resp.json()["job_ids"][0]

    resp = await api.get(f"/jobs/{job_id}/events", headers={"Authorization": f"Bearer {token_b}"})
    assert resp.status_code == 404


# --------------------------------------------------- readiness dependency probe


async def test_readyz_reports_ml_reachability(api: AsyncClient) -> None:
    """/readyz must say something about ml, over the network the worker uses.

    Regression test for a real blind spot: ml's container healthcheck probes
    localhost from inside the container, the worker has no probe, and /readyz
    used to check only postgres and redis. An ml container detached from the
    compose network therefore reported healthy on its published port while
    every worker call failed with ConnectError — for half an hour, with no
    signal anywhere.
    """
    resp = await api.get("/readyz")
    body = resp.json()
    assert "dependencies" in body, body
    assert "ml" in body["dependencies"], body
    # Either reachable, or a string that names WHY not — never silence.
    assert body["dependencies"]["ml"]


async def test_ml_being_down_does_not_fail_api_readiness(
    api: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ml down is a DEGRADATION, not an API outage.

    Ingest absorbs an ml outage by design (Unavailable backpressure, then
    DEGRADED_TAGGED), and reads, corrections and boards do not touch ml at all.
    If ml gated readiness, an outage it is built to survive would pull every
    API pod out of rotation instead — turning a partial degradation into a
    total one.
    """
    from stylist_api.routers import health

    monkeypatch.setattr(health, "ML_BASE_URL", "http://127.0.0.1:1")  # nothing listens
    resp = await api.get("/readyz")
    body = resp.json()
    assert body["dependencies"]["ml"].startswith("unreachable:"), body
    # Postgres and redis are up, so readiness holds.
    assert body["ready"] is True, body
    assert resp.status_code == 200, resp.status_code


# --------------------------------------------------------------------- CORS
#
# These assert HEADERS, not browser behaviour. CORS is enforced by the browser,
# never by the server, which is exactly why the API shipped with no CORS
# middleware at all and every server-side test still passed: httpx does not
# care. The only symptom was "TypeError: Failed to fetch" in the UI, naming
# neither the cause nor the service.


async def test_preflight_from_the_web_origin_is_allowed(api: AsyncClient) -> None:
    resp = await api.options(
        "/auth/register",
        headers={
            "Origin": "http://localhost:3100",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status_code == 200, resp.status_code
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3100"


async def test_ingest_preflight_allows_the_idempotency_key_header(api: AsyncClient) -> None:
    """The trap this class of bug sets.

    Ingest REQUIRES Idempotency-Key. Omit it from allow_headers and auth works
    while every upload fails at the preflight — a partial break that looks like
    a bug in the upload code rather than in CORS configuration.
    """
    resp = await api.options(
        "/garments/ingest",
        headers={
            "Origin": "http://localhost:3100",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
        },
    )
    assert resp.status_code == 200, resp.status_code
    allowed = resp.headers.get("access-control-allow-headers", "").lower()
    assert "idempotency-key" in allowed, allowed
    assert "authorization" in allowed, allowed


async def test_an_unlisted_origin_gets_no_allow_origin_header(api: AsyncClient) -> None:
    """The allowlist has to actually exclude things.

    A wildcard would pass the two tests above while making the API callable by
    any page the user happens to visit.
    """
    resp = await api.options(
        "/auth/register",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.headers.get("access-control-allow-origin") is None, dict(resp.headers)
