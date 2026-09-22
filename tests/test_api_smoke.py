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
import uuid

import pytest
from httpx import AsyncClient

from tests.conftest import FakeObjectStore, _register, _upload_one  # noqa: F401

pytestmark = pytest.mark.asyncio


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


# ------------------------------------------------------------ admin boundary
#
# `/ops` serves CROSS-TENANT aggregates through SECURITY DEFINER functions
# that deliberately bypass RLS. `routers/ops.py` had said since Phase 5 that it
# "belongs behind an admin authorisation boundary rather than a user token";
# migration 0019 built it. These tests are what stop it being removed by
# accident, because nothing else would notice.


async def test_ops_is_refused_to_an_ordinary_account(api, registered) -> None:
    """The gap this closes: any registered user could read the whole
    deployment's ingest funnel, latency, DLQ depth and model spend."""
    for path in ("/ops/alerts", "/ops/dashboards", "/ops/rerank"):
        resp = await api.get(path, headers=registered.auth)
        assert resp.status_code == 403, f"{path} must not answer an ordinary account"


async def test_tenant_scoped_ops_stays_open(api, registered) -> None:
    """`/ops/correction-rate` is deliberately tenant-scoped — it reports on the
    caller's OWN garments — so gating it would remove a legitimate feature in
    the name of a boundary it does not need."""
    resp = await api.get("/ops/correction-rate", headers=registered.auth)
    assert resp.status_code == 200


async def test_admin_is_not_grantable_through_the_api(api, registered) -> None:
    """An API that can escalate its own callers defeats the boundary entirely:
    compromise one account, call one endpoint, read everyone's data. There is
    no such endpoint, and this asserts it stays that way."""
    paths = [r.path for r in api._transport.app.routes if hasattr(r, "path")]  # type: ignore[attr-defined]
    grants = [p for p in paths if "admin" in p.lower()]
    assert not grants, f"no route may grant admin; found {grants}"


async def test_put_survives_a_browser_preflight(api) -> None:
    """CORS must allow every method the UI actually uses.

    PUT was missing from `allow_methods`, and the failure was invisible from
    the server side: a browser preflights PUT, gets 400 "Disallowed CORS
    method", and `fetch` throws "Failed to fetch" — a network error with no
    status, no body and NOTHING in the API log, because the request the app
    cared about was never made.

    Every PUT endpoint therefore passed its own tests (httpx does not
    preflight) and was dead in the browser: `PUT /me/location` silently never
    saved a city, and `PUT /me/avatar` reported "Failed to fetch".

    Asserted per METHOD rather than by reading the header, so adding a route
    with a method nobody allowed fails here instead of in someone's browser.
    """
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        resp = await api.options(
            "/me/avatar",
            headers={
                "Origin": "http://localhost:3100",
                "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert resp.status_code == 200, f"{method} is refused by the CORS preflight"
