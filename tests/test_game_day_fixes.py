"""Fixes for the 2026-09-15 game day findings.

Each test names the scenario that produced it. These are the three things a
deliberate outage revealed that no unit test had — which is the argument for
running the exercise rather than reasoning about it.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import InterfaceError, OperationalError


def test_a_dependency_outage_is_a_503_not_a_500() -> None:
    """GAME DAY FINDING 1.

    With Postgres stopped, every endpoint returned 500 — `/suggestions`,
    `/auth/login`, all of it. That is the wrong instruction to everyone reading
    it: a 500 says "we have a bug" (do not retry, page someone, read a
    traceback), a 503 says "the thing we depend on is down" (retry with
    backoff, the traceback will not help).

    It also left `api_5xx` unable to tell a database outage from a bad deploy,
    which is the first question that alert's runbook asks.
    """
    import socket

    from stylist_api.main import create_app

    app = create_app()
    handled = set(app.exception_handlers)
    assert OperationalError in handled, "a connection failure must map to 503"
    assert InterfaceError in handled, "a dropped connection must map to 503"
    # THE SQLALCHEMY TYPES ALONE WERE NOT ENOUGH, and this assertion is the
    # only reason that is not still true. Retested against a stopped Postgres:
    # the exception reaching the handler was a raw
    # `socket.gaierror: [Errno -2] Name or service not known` — DNS failing
    # before a connection exists, so there is nothing for SQLAlchemy to wrap.
    # Registering only the ORM exceptions looked right, passed a structural
    # test, and still returned 500 to every request.
    assert socket.gaierror in handled, "DNS failure is a dependency outage"
    assert ConnectionError in handled, "refused connection is a dependency outage"
    # OSError itself must NOT be registered: a missing file or a full disk is
    # not "retry shortly".
    assert OSError not in handled


def test_a_sql_bug_still_returns_500() -> None:
    """The counterweight, and the reason this is registered per exception type
    rather than on `Exception`.

    A `ProgrammingError` is OUR SQL being wrong. Turning that into a retryable
    503 is how a broken query becomes an infinite retry loop that looks like a
    dependency problem.
    """
    from sqlalchemy.exc import ProgrammingError

    from stylist_api.main import create_app

    assert ProgrammingError not in set(create_app().exception_handlers)


@pytest.mark.asyncio
async def test_a_cache_outage_does_not_fail_readiness(api) -> None:
    """GAME DAY FINDING 3.

    With `redis-cache` stopped, `/readyz` returned 503 while `/suggestions`
    returned 200. A load balancer reading that probe would pull a working
    instance out of rotation and turn a cache outage into a total one — the
    exact failure the ml decision exists to prevent, applied inconsistently to
    the dependency next to it.

    Asserted structurally: `redis_cache` must be reported under
    `dependencies` (informational) and NOT under `checks` (fatal).
    """
    body = (await api.get("/readyz")).json()

    assert "redis_cache" in body["dependencies"], "still reported — a cold cache matters"
    assert "redis_cache" not in body["checks"], "but not fatal to readiness"
    # The genuinely fatal ones stay fatal.
    assert "postgres" in body["checks"]
    assert "redis_queue" in body["checks"], (
        "an upload accepted into a queue nobody can read is worse than one refused"
    )


def test_the_container_healthcheck_probes_readiness_not_liveness() -> None:
    """GAME DAY FINDING 2.

    The api container reported `Up (healthy)` for the entire Postgres outage,
    because `/healthz` is liveness and deliberately touches no dependency. That
    is correct for a restart policy and useless as the thing a human glances
    at, which is exactly what compose's status column is.
    """
    import pathlib

    import yaml

    compose = yaml.safe_load(
        (
            pathlib.Path(__file__).resolve().parents[1] / "infra/compose/docker-compose.yml"
        ).read_text()
    )
    probe = " ".join(compose["services"]["api"]["healthcheck"]["test"])

    assert "/readyz" in probe
    assert "/healthz" not in probe
