"""The four alerts must fire. An alert that cannot fire is worse than none.

The first version of the ops router queried `jobs` directly. The API runs as
`stylist_app`, which is NOBYPASSRLS, and every tenant table is FORCE RLS — so
those queries returned zero rows with no error, and `count(*) = 0` reads as
"the DLQ is empty". The alerts were permanently green and structurally
incapable of firing.

Nothing about that is visible from a passing health check, so these tests
assert the firing direction explicitly: each one arranges the bad condition and
demands the alert notices.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from stylist_api.routers.ops import (
    API_5XX_CEILING,
    DLQ_AGE_ALERT_SECONDS,
    INGEST_SUCCESS_FLOOR,
    MIN_SAMPLE_FOR_RATE,
)

pytestmark = pytest.mark.asyncio


def _alert(body: dict, name: str) -> dict:
    return next(a for a in body["alerts"] if a["alert"] == name)


async def test_ops_aggregates_see_across_tenants(
    api: AsyncClient, registered, owner_engine
) -> None:
    """The regression test for the bug that made every alert blind.

    A job belonging to a tenant must be counted by the ops aggregate even
    though the API's own role cannot select that row.
    """
    async with owner_engine.begin() as conn:
        before = (await conn.execute(text("SELECT total FROM ops_ingest_stats(24)"))).scalar_one()
        uid = (await conn.execute(text("SELECT id FROM users LIMIT 1"))).scalar_one()
        await conn.execute(
            text(
                """
                INSERT INTO jobs (id, user_id, kind, state, payload, created_at, updated_at)
                VALUES (:id, :uid, 'ingest_photo', 'received', '{}'::jsonb, now(), now())
                """
            ),
            {"id": uuid.uuid4(), "uid": uid},
        )
        after = (await conn.execute(text("SELECT total FROM ops_ingest_stats(24)"))).scalar_one()
    assert after == before + 1, "the ops aggregate cannot see rows it must count"


async def test_dlq_age_fires_when_a_job_is_stuck(
    api: AsyncClient, registered, owner_engine
) -> None:
    async with owner_engine.begin() as conn:
        uid = (await conn.execute(text("SELECT id FROM users LIMIT 1"))).scalar_one()
        await conn.execute(
            text(
                """
                INSERT INTO jobs (id, user_id, kind, state, payload, dlq_at, created_at, updated_at)
                VALUES (:id, :uid, 'ingest_photo', 'sanitised', '{}'::jsonb,
                        now() - make_interval(secs => :age), now(), now())
                """
            ),
            {"id": uuid.uuid4(), "uid": uid, "age": DLQ_AGE_ALERT_SECONDS + 600},
        )
    try:
        body = (await api.get("/ops/alerts", headers=registered.auth)).json()
        dlq = _alert(body, "dlq_age")
        assert dlq["firing"] is True, dlq
        assert dlq["value_seconds"] > DLQ_AGE_ALERT_SECONDS
        assert body["ok"] is False
        # Every alert names what to do about it; one with no response is a
        # notification wearing an alert's clothes.
        assert dlq["response"]
    finally:
        async with owner_engine.begin() as conn:
            await conn.execute(text("DELETE FROM jobs WHERE dlq_at IS NOT NULL"))


async def test_dlq_age_is_quiet_when_nothing_is_stuck(
    api: AsyncClient, registered, owner_engine
) -> None:
    """The other half. An alert that always fires is also useless."""
    async with owner_engine.begin() as conn:
        await conn.execute(text("DELETE FROM jobs WHERE dlq_at IS NOT NULL"))
    body = (await api.get("/ops/alerts", headers=registered.auth)).json()
    assert _alert(body, "dlq_age")["firing"] is False


async def test_ingest_success_ignores_in_flight_jobs(
    api: AsyncClient, registered, owner_engine
) -> None:
    """A burst of queued work must not look like a failure.

    Counting unsettled jobs against the rate makes it dip every time traffic
    arrives — a page at precisely the moment nothing is wrong.
    """
    async with owner_engine.begin() as conn:
        await conn.execute(text("DELETE FROM jobs WHERE dlq_at IS NOT NULL"))
        uid = (await conn.execute(text("SELECT id FROM users LIMIT 1"))).scalar_one()
        for _ in range(30):
            await conn.execute(
                text(
                    """
                    INSERT INTO jobs (id, user_id, kind, state, payload, created_at, updated_at)
                    VALUES (:id, :uid, 'ingest_photo', 'sanitised', '{}'::jsonb, now(), now())
                    """
                ),
                {"id": uuid.uuid4(), "uid": uid},
            )
    body = (await api.get("/ops/alerts", headers=registered.auth)).json()
    ing = _alert(body, "ingest_success")
    assert ing["in_flight"] >= 30
    assert ing["firing"] is False, ing


async def test_ingest_success_fires_when_jobs_actually_fail(
    api: AsyncClient, registered, owner_engine
) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(text("DELETE FROM jobs"))
        uid = (await conn.execute(text("SELECT id FROM users LIMIT 1"))).scalar_one()
        # Enough settled jobs to clear the noise floor, with >1% dead.
        for i in range(MIN_SAMPLE_FOR_RATE + 10):
            dead = i % 5 == 0
            await conn.execute(
                text(
                    """
                    INSERT INTO jobs (id, user_id, kind, state, payload,
                                      dlq_at, created_at, updated_at)
                    VALUES (:id, :uid, 'ingest_photo', :state, '{}'::jsonb,
                            CASE WHEN :dead THEN now() ELSE NULL END, now(), now())
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "uid": uid,
                    "state": "sanitised" if dead else "complete",
                    "dead": dead,
                },
            )
    try:
        body = (await api.get("/ops/alerts", headers=registered.auth)).json()
        ing = _alert(body, "ingest_success")
        assert ing["rate"] is not None and ing["rate"] < INGEST_SUCCESS_FLOOR, ing
        assert ing["firing"] is True, ing
    finally:
        async with owner_engine.begin() as conn:
            await conn.execute(text("DELETE FROM jobs"))


async def test_a_rate_over_too_few_samples_does_not_fire(
    api: AsyncClient, registered, owner_engine
) -> None:
    """2 failures out of 3 is 67% and means nothing.

    Alerting on tiny samples is how a team learns to ignore the alert.
    """
    async with owner_engine.begin() as conn:
        await conn.execute(text("DELETE FROM jobs"))
        uid = (await conn.execute(text("SELECT id FROM users LIMIT 1"))).scalar_one()
        for _ in range(3):
            await conn.execute(
                text(
                    """
                    INSERT INTO jobs (id, user_id, kind, state, payload,
                                      dlq_at, created_at, updated_at)
                    VALUES (:id, :uid, 'ingest_photo', 'sanitised', '{}'::jsonb,
                            now(), now(), now())
                    """
                ),
                {"id": uuid.uuid4(), "uid": uid},
            )
    try:
        body = (await api.get("/ops/alerts", headers=registered.auth)).json()
        ing = _alert(body, "ingest_success")
        assert ing["rate"] == 0.0
        assert ing["firing"] is False, "fired on 3 samples"
    finally:
        async with owner_engine.begin() as conn:
            await conn.execute(text("DELETE FROM jobs"))


async def test_the_middleware_actually_counts_requests(api: AsyncClient, registered) -> None:
    """The counter write must reach Redis.

    It did not for the first version: the middleware called `.pipeline()` on
    the CacheRedis wrapper, which has no such method, and its own except-clause
    swallowed the AttributeError — because telemetry must never fail a request.
    The result was a permanently empty counter and an api_5xx alert that could
    not fire.
    """
    for _ in range(5):
        await api.get("/garments", headers=registered.auth)
    body = (await api.get("/ops/alerts", headers=registered.auth)).json()
    counts = _alert(body, "api_5xx")["counts"]
    assert sum(counts.values()) > 0, f"no requests counted: {counts}"
    assert "2xx" in counts, counts


async def test_ops_endpoints_are_not_counted_by_the_middleware(
    api: AsyncClient, registered
) -> None:
    """A monitoring loop must not dilute the rate it is measuring."""
    before = (await api.get("/ops/alerts", headers=registered.auth)).json()
    n_before = sum(_alert(before, "api_5xx")["counts"].values())
    for _ in range(3):
        await api.get("/ops/alerts", headers=registered.auth)
    after = (await api.get("/ops/alerts", headers=registered.auth)).json()
    assert sum(_alert(after, "api_5xx")["counts"].values()) == n_before


async def test_thresholds_match_the_plan() -> None:
    """The plan names these four numbers; drift should be deliberate."""
    assert DLQ_AGE_ALERT_SECONDS == 3600.0
    assert INGEST_SUCCESS_FLOOR == 0.99
    assert API_5XX_CEILING == 0.01
