"""Ingest state machine: resume, per-stage retry, DLQ (View 5).

Three Phase 2 exit criteria live in this file:
  - crash mid-pipeline -> resumes from the last state, does NOT re-run
    completed stages
  - poison image -> 3 attempts -> DLQ, alert fires, other jobs unaffected
  - retries are counted PER STAGE, so a late failure never re-runs an early
    expensive stage

Stages here are fakes that count their own calls. That is the point: the unit
under test is the machine's control flow, and a fake that records invocations
is the only way to assert "segmentation did not run again", which is the whole
reason per-stage retry exists.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from stylist_worker.state_machine import (
    MAX_ATTEMPTS,
    STATE_ORDER,
    UNAVAILABLE_BUDGET_SECONDS,
    Exhausted,
    IngestState,
    JobContext,
    Stage,
    Terminal,
    Unavailable,
    run_pipeline,
    state_rank,
)

# asyncio_mode=auto (pyproject) already collects coroutine tests, so no
# module-level asyncio mark: applying one makes pytest-asyncio warn about
# the sync tests in this file.


class CountingStage:
    """A stage that records every invocation and can be told to fail."""

    def __init__(self, name: str, *, fail_times: int = 0, terminal: IngestState | None = None):
        self.name = name
        self.calls = 0
        self.fail_times = fail_times
        self.terminal = terminal

    async def __call__(self, ctx: JobContext) -> dict[str, Any]:
        self.calls += 1
        if self.terminal is not None:
            raise Terminal(self.terminal, f"{self.name} says stop")
        if self.calls <= self.fail_times:
            raise RuntimeError(f"{self.name} transient failure {self.calls}")
        return {f"{self.name}_done": True}


@pytest.fixture
def make_stages():
    def _make(**overrides: CountingStage) -> tuple[tuple[Stage, ...], dict[str, CountingStage]]:
        specs = [
            ("validate", IngestState.VALIDATED),
            ("sanitise", IngestState.SANITISED),
            ("moderate", IngestState.MODERATED),
            ("matte", IngestState.MATTED),
            ("persist", IngestState.COMPLETE),
        ]
        fns: dict[str, CountingStage] = {}
        stages = []
        for name, completed in specs:
            fn = overrides.get(name) or CountingStage(name)
            fns[name] = fn
            stages.append(Stage(name=name, completed_state=completed, run=fn, retryable=True))
        return tuple(stages), fns

    return _make


async def _make_job(owner_engine, state: str = "received") -> tuple[uuid.UUID, uuid.UUID]:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    user_id, job_id, garment_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with maker() as session, session.begin():
        await session.execute(
            text("INSERT INTO users (id, email, password_hash) VALUES (:id, :e, 'x')"),
            {"id": user_id, "e": f"sm-{user_id}@example.com"},
        )
        await session.execute(
            text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(user_id)}
        )
        await session.execute(
            text(
                "INSERT INTO garments (id, user_id, original_key, state) "
                "VALUES (:g, :uid, 'k', 'received')"
            ),
            {"g": garment_id, "uid": user_id},
        )
        await session.execute(
            text(
                "INSERT INTO jobs (id, user_id, kind, state, garment_id, payload) "
                "VALUES (:j, :uid, 'ingest', :st, :g, '{\"key\": \"k\"}')"
            ),
            {"j": job_id, "uid": user_id, "st": state, "g": garment_id},
        )
    return job_id, user_id


async def _cleanup(owner_engine, user_id: uuid.UUID) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})


async def _job_row(owner_engine, job_id: uuid.UUID) -> dict[str, Any]:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with maker() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT state, stage_attempts, dlq_at, last_error, dependency_wait_s, "
                        "deferrals FROM jobs WHERE id = :id"
                    ),
                    {"id": job_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


# --------------------------------------------------------------------------


def test_state_order_has_no_duplicates() -> None:
    """Two stages sharing a completed_state silently skips one of them — the
    bug that made `persist` never run when it also claimed MATTED."""
    assert len(STATE_ORDER) == len(set(STATE_ORDER))


def test_off_ramp_states_rank_below_everything() -> None:
    assert state_rank("rejected") == -1
    assert state_rank("received") == 0
    assert state_rank("complete") == len(STATE_ORDER) - 1


async def test_happy_path_runs_every_stage_once(owner_engine, make_stages) -> None:
    job_id, user_id = await _make_job(owner_engine)
    stages, fns = make_stages()
    try:
        final = await run_pipeline(user_id, job_id, stages)
        assert final == "complete"
        assert all(fn.calls == 1 for fn in fns.values()), {n: f.calls for n, f in fns.items()}
    finally:
        await _cleanup(owner_engine, user_id)


async def test_resume_skips_completed_stages(owner_engine, make_stages) -> None:
    """PHASE 2 EXIT CRITERION: restart resumes, it does not re-run.

    The job is seeded already at `moderated`, standing in for a worker that
    died after moderation. Only matte and persist may run — re-running matte's
    predecessors would mean re-paying for the expensive stages on every
    transient late failure.
    """
    job_id, user_id = await _make_job(owner_engine, state="moderated")
    stages, fns = make_stages()
    try:
        final = await run_pipeline(user_id, job_id, stages)
        assert final == "complete"
        assert fns["validate"].calls == 0, "re-ran validate after resume"
        assert fns["sanitise"].calls == 0, "re-ran sanitise after resume"
        assert fns["moderate"].calls == 0, "re-ran moderate after resume"
        assert fns["matte"].calls == 1
        assert fns["persist"].calls == 1
    finally:
        await _cleanup(owner_engine, user_id)


async def test_crash_midway_then_rerun_completes_without_redoing_work(
    owner_engine, make_stages
) -> None:
    """The real crash shape: matte blows up hard, a new worker picks the job up.

    Asserted across two run_pipeline calls with the SAME stage objects, so the
    call counts span the 'restart'.
    """
    job_id, user_id = await _make_job(owner_engine)
    # Fails more times than the retry budget, so the first run gives up.
    matte = CountingStage("matte", fail_times=MAX_ATTEMPTS)
    stages, fns = make_stages(matte=matte)
    try:
        first = await run_pipeline(user_id, job_id, stages)
        assert first == "dlq"
        row = await _job_row(owner_engine, job_id)
        assert row["state"] == "moderated", "DLQ should preserve the last GOOD state"
        assert row["dlq_at"] is not None

        # Operator clears the DLQ flag (Phase 5 gives this a UI) and it re-runs.
        from sqlalchemy.ext.asyncio import async_sessionmaker

        maker = async_sessionmaker(owner_engine, expire_on_commit=False)
        async with maker() as session, session.begin():
            await session.execute(
                text("UPDATE jobs SET dlq_at = NULL, stage_attempts = '{}' WHERE id = :id"),
                {"id": job_id},
            )

        second = await run_pipeline(user_id, job_id, stages)
        assert second == "complete"
        # The three stages before matte ran exactly once, across both runs.
        assert fns["validate"].calls == 1
        assert fns["sanitise"].calls == 1
        assert fns["moderate"].calls == 1
    finally:
        await _cleanup(owner_engine, user_id)


async def test_retry_is_per_stage_not_per_job(owner_engine, make_stages) -> None:
    """A late transient failure must not re-run early stages.

    matte fails twice then succeeds. If retries were per-JOB, validate and
    sanitise would each have run three times — a 3x CPU bill for one flaky
    dependency.
    """
    job_id, user_id = await _make_job(owner_engine)
    matte = CountingStage("matte", fail_times=2)
    stages, fns = make_stages(matte=matte)
    try:
        final = await run_pipeline(user_id, job_id, stages)
        assert final == "complete"
        assert matte.calls == 3, "expected 2 failures + 1 success"
        assert fns["validate"].calls == 1, "per-job retry would have re-run validate"
        assert fns["sanitise"].calls == 1
        assert fns["moderate"].calls == 1
    finally:
        await _cleanup(owner_engine, user_id)


async def test_attempts_persist_across_worker_restarts(owner_engine, make_stages) -> None:
    """Otherwise a permanently-failing stage never reaches the DLQ: each new
    worker hands it a fresh budget of 3 and it retries forever."""
    job_id, user_id = await _make_job(owner_engine)
    matte = CountingStage("matte", fail_times=99)
    stages, _ = make_stages(matte=matte)
    try:
        await run_pipeline(user_id, job_id, stages)
        row = await _job_row(owner_engine, job_id)
        assert row["stage_attempts"].get("matte") == MAX_ATTEMPTS
        assert row["dlq_at"] is not None
    finally:
        await _cleanup(owner_engine, user_id)


async def test_terminal_stage_is_never_retried(owner_engine, make_stages) -> None:
    """A corrupt file fails identically every time; retrying wastes CPU and
    delays telling the user."""
    job_id, user_id = await _make_job(owner_engine)
    validate = CountingStage("validate", terminal=IngestState.REJECTED)
    stages, fns = make_stages(validate=validate)
    try:
        final = await run_pipeline(user_id, job_id, stages)
        assert final == "rejected"
        assert validate.calls == 1, "a Terminal verdict was retried"
        assert fns["matte"].calls == 0, "pipeline continued past a terminal state"
    finally:
        await _cleanup(owner_engine, user_id)


async def test_dlq_fires_an_alert(owner_engine, make_stages, caplog) -> None:
    """PHASE 2 EXIT CRITERION: the alert has to be observable, not a TODO."""
    job_id, user_id = await _make_job(owner_engine)
    stages, _ = make_stages(matte=CountingStage("matte", fail_times=99))
    try:
        with caplog.at_level(logging.ERROR, logger="stylist.alerts"):
            await run_pipeline(user_id, job_id, stages)
        alerts = [r for r in caplog.records if getattr(r, "alert", None) == "dlq.job_parked"]
        assert alerts, "no dlq.job_parked alert was emitted"
        assert str(job_id) in alerts[0].getMessage()
    finally:
        await _cleanup(owner_engine, user_id)


async def test_one_poisoned_job_does_not_affect_another(owner_engine, make_stages) -> None:
    """PHASE 2 EXIT CRITERION: other jobs unaffected.

    Blast-radius containment at the job level: a poison image parks itself and
    nothing else notices.
    """
    bad_job, bad_user = await _make_job(owner_engine)
    good_job, good_user = await _make_job(owner_engine)
    try:
        bad_stages, _ = make_stages(matte=CountingStage("matte", fail_times=99))
        good_stages, good_fns = make_stages()

        assert await run_pipeline(bad_user, bad_job, bad_stages) == "dlq"
        assert await run_pipeline(good_user, good_job, good_stages) == "complete"

        assert (await _job_row(owner_engine, bad_job))["dlq_at"] is not None
        assert (await _job_row(owner_engine, good_job))["dlq_at"] is None
        assert all(fn.calls == 1 for fn in good_fns.values())
    finally:
        await _cleanup(owner_engine, bad_user)
        await _cleanup(owner_engine, good_user)


async def test_rerunning_a_complete_job_is_a_noop(owner_engine, make_stages) -> None:
    """A duplicate delivery from the relay must cost a row read, not a matte."""
    job_id, user_id = await _make_job(owner_engine, state="complete")
    stages, fns = make_stages()
    try:
        final = await run_pipeline(user_id, job_id, stages)
        assert final == "complete"
        assert all(fn.calls == 0 for fn in fns.values())
    finally:
        await _cleanup(owner_engine, user_id)


async def test_a_dlq_job_is_not_picked_up_again(owner_engine, make_stages) -> None:
    job_id, user_id = await _make_job(owner_engine)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        await session.execute(text("UPDATE jobs SET dlq_at = now() WHERE id = :id"), {"id": job_id})
    stages, fns = make_stages()
    try:
        assert await run_pipeline(user_id, job_id, stages) == "skipped"
        assert all(fn.calls == 0 for fn in fns.values())
    finally:
        await _cleanup(owner_engine, user_id)


def test_exhausted_carries_diagnostics() -> None:
    exc = Exhausted("matte", 3, "boom")
    assert exc.stage == "matte"
    assert exc.attempts == 3
    assert "boom" in str(exc)


class UnavailableStage:
    """A stage whose dependency is down for the first N calls."""

    def __init__(self, name: str, *, unavailable_times: int, retry_after: float = 0.01):
        self.name = name
        self.calls = 0
        self.unavailable_times = unavailable_times
        self.retry_after = retry_after

    async def __call__(self, ctx: JobContext) -> dict[str, Any]:
        self.calls += 1
        if self.calls <= self.unavailable_times:
            raise Unavailable(f"{self.name} dependency down", retry_after=self.retry_after)
        return {f"{self.name}_done": True}


async def test_an_unavailable_dependency_does_not_consume_retry_attempts(
    owner_engine, make_stages
) -> None:
    """THE BUG THIS GUARDS, measured against the real system:

    ml takes ~30s to build its ONNX sessions. The attempt budget is 3 tries
    with full-jitter backoff off a 2s base, which exhausts in ~6s. So an
    ordinary `docker compose restart ml` DLQ'd every in-flight ingest with
    `stage_attempts={"matte": 3}` and a ConnectError — users losing uploads
    because a new model shipped.

    A dependency being down says nothing about the image, so it must not spend
    the image's budget.
    """
    job_id, user_id = await _make_job(owner_engine)
    # More rounds than MAX_ATTEMPTS: under the old rule this was a guaranteed DLQ.
    matte = UnavailableStage("matte", unavailable_times=6)
    stages, _ = make_stages(matte=matte)
    try:
        final = await run_pipeline(user_id, job_id, stages)
        assert final == "complete", "an ml restart still fails the job"
        assert matte.calls == 7, "expected 6 unavailable rounds then a success"

        row = await _job_row(owner_engine, job_id)
        assert row["dlq_at"] is None, "job was parked in the DLQ by a dependency restart"
        assert "matte" not in row["stage_attempts"], (
            f"unavailability consumed retry attempts: {row['stage_attempts']}"
        )
    finally:
        await _cleanup(owner_engine, user_id)


async def test_a_permanently_unavailable_dependency_eventually_dlqs(
    owner_engine, make_stages, monkeypatch
) -> None:
    """The budget is bounded: "wait forever" would hide a real outage.

    Past the wall-clock budget it IS an outage, and the DLQ is where the job
    belongs — replayable from its last good state once the dependency is back.
    """
    import stylist_worker.state_machine as sm

    monkeypatch.setattr(sm, "UNAVAILABLE_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(sm, "UNAVAILABLE_BACKOFF_CAP_SECONDS", 0.01)

    job_id, user_id = await _make_job(owner_engine)
    stages, _ = make_stages(matte=UnavailableStage("matte", unavailable_times=9999))
    try:
        assert await run_pipeline(user_id, job_id, stages) == "dlq"
        row = await _job_row(owner_engine, job_id)
        assert row["dlq_at"] is not None
        assert row["state"] == "moderated", "DLQ must preserve the last good state"
    finally:
        await _cleanup(owner_engine, user_id)


async def test_unavailability_honours_the_servers_retry_after(owner_engine, make_stages) -> None:
    """A service that knows when it will be ready should be believed over our
    backoff guess — that is what Retry-After is for."""
    job_id, user_id = await _make_job(owner_engine)
    matte = UnavailableStage("matte", unavailable_times=2, retry_after=0.02)
    stages, _ = make_stages(matte=matte)
    try:
        import time

        t0 = time.monotonic()
        assert await run_pipeline(user_id, job_id, stages) == "complete"
        elapsed = time.monotonic() - t0
        # Two waits of 20ms each. Without honouring Retry-After the default
        # ladder would be 2s then 4s, so this would take seconds.
        assert elapsed < 1.0, f"ignored Retry-After; took {elapsed:.2f}s"
    finally:
        await _cleanup(owner_engine, user_id)


async def test_a_real_failure_still_consumes_attempts(owner_engine, make_stages) -> None:
    """Guard the other direction: the fix must not make everything infinitely
    retryable. A genuine error still burns an attempt and still reaches the DLQ.
    """
    job_id, user_id = await _make_job(owner_engine)
    stages, _ = make_stages(matte=CountingStage("matte", fail_times=99))
    try:
        assert await run_pipeline(user_id, job_id, stages) == "dlq"
        row = await _job_row(owner_engine, job_id)
        assert row["stage_attempts"].get("matte") == MAX_ATTEMPTS
    finally:
        await _cleanup(owner_engine, user_id)


def test_the_unavailable_budget_exceeds_a_realistic_cold_start() -> None:
    """The number has to beat the thing it exists to survive.

    Measured: the ml service needs ~30s to build three ONNX sessions. A budget
    below that would make this whole mechanism decorative.
    """
    assert UNAVAILABLE_BUDGET_SECONDS >= 120.0


# ------------------------------------- deferral: giving the worker slot back


async def test_a_long_dependency_wait_defers_instead_of_holding_the_slot(
    owner_engine, make_stages, monkeypatch
) -> None:
    """THE BUG THIS FIXES.

    `Unavailable` used to be absorbed by sleeping in-process for up to
    UNAVAILABLE_BUDGET_SECONDS (180s). That is right about the backpressure and
    wrong about where to wait: arq runs WORKER_MAX_JOBS=2, so two jobs waiting
    on a down ml service occupied both slots and the queue stopped draining for
    every other tenant. §C6 asks for "workers backoff; queue absorbs" and this
    absorbed nothing.

    Known since the cross-phase pass on 2026-09-10 and seen twice on
    2026-09-17: once as a `TimeoutError` when the 180s sleep outlived arq's own
    job ceiling, and once as ten parked jobs starving the queue.

    Past the in-process budget the job must hand the slot back, and it must do
    so FAST — the assertion below is on elapsed time, because a test that only
    checked the exception type would pass even if the sleep were still there.
    """
    import time

    import stylist_worker.state_machine as sm

    monkeypatch.setattr(sm, "IN_PROCESS_WAIT_BUDGET_SECONDS", 0.0)

    job_id, user_id = await _make_job(owner_engine)
    stages, _ = make_stages(matte=UnavailableStage("matte", unavailable_times=9999, retry_after=30))
    try:
        t0 = time.monotonic()
        with pytest.raises(sm.Deferred) as caught:
            await run_pipeline(user_id, job_id, stages)
        elapsed = time.monotonic() - t0

        assert elapsed < 1.0, f"held the slot for {elapsed:.2f}s instead of deferring"
        assert caught.value.delay == 30, "must propagate the server's Retry-After"

        row = await _job_row(owner_engine, job_id)
        assert row["dlq_at"] is None, "a deferral is not a failure"
        assert row["state"] == "moderated", "the job keeps its last good state"
        assert "matte" not in row["stage_attempts"], "a deferral must not consume an attempt"
    finally:
        await _cleanup(owner_engine, user_id)


async def test_the_wait_survives_the_re_enqueue(owner_engine, make_stages, monkeypatch) -> None:
    """The accumulated wait is persisted WITH the delay about to be spent
    outside the process, so the accounting is identical whether the job waited
    in the worker or in the queue. That equivalence is the whole reason the
    budget still means anything after a deferral."""
    import stylist_worker.state_machine as sm

    monkeypatch.setattr(sm, "IN_PROCESS_WAIT_BUDGET_SECONDS", 0.0)

    job_id, user_id = await _make_job(owner_engine)
    stages, _ = make_stages(matte=UnavailableStage("matte", unavailable_times=9999, retry_after=12))
    try:
        with pytest.raises(sm.Deferred):
            await run_pipeline(user_id, job_id, stages)
        row = await _job_row(owner_engine, job_id)
        assert row["dependency_wait_s"] == 12, "the wait was not persisted"
        assert row["deferrals"] == 1
        assert "deferred" in (row["last_error"] or ""), (
            "a deferred job must say why, or it is indistinguishable from a stuck one"
        )
    finally:
        await _cleanup(owner_engine, user_id)


async def test_a_permanently_down_dependency_still_dlqs_across_deferrals(
    owner_engine, make_stages, monkeypatch
) -> None:
    """THE REGRESSION THIS EXISTS FOR, and the reason the wait is a column.

    If each re-enqueue started the budget at zero, a permanently-down
    dependency would defer forever and NEVER reach the DLQ — trading a stalled
    queue for an invisible infinite retry, which is worse because nothing
    alerts on it. This drives the loop the worker drives and asserts the job
    lands in the DLQ anyway.
    """
    import stylist_worker.state_machine as sm

    monkeypatch.setattr(sm, "IN_PROCESS_WAIT_BUDGET_SECONDS", 0.0)
    monkeypatch.setattr(sm, "UNAVAILABLE_BUDGET_SECONDS", 25.0)
    monkeypatch.setattr(sm, "UNAVAILABLE_BACKOFF_CAP_SECONDS", 10.0)

    job_id, user_id = await _make_job(owner_engine)
    stages, _ = make_stages(matte=UnavailableStage("matte", unavailable_times=9999, retry_after=10))
    try:
        outcome = None
        for _ in range(10):  # the worker's loop, without the waiting
            try:
                outcome = await run_pipeline(user_id, job_id, stages)
                break
            except sm.Deferred:
                continue
        assert outcome == "dlq", f"never reached the DLQ; ended at {outcome!r}"

        row = await _job_row(owner_engine, job_id)
        assert row["dlq_at"] is not None
        assert row["state"] == "moderated", "DLQ must preserve the last good state"
        assert row["deferrals"] >= 2, "expected several deferrals before the budget ran out"
    finally:
        await _cleanup(owner_engine, user_id)


async def test_each_deferral_gets_a_distinct_arq_job_id(owner_engine, make_stages) -> None:
    """arq DEDUPES ON `_job_id`, and that is a trap here.

    Reusing the relay's `outbox-{id}` returns None as already-completed and the
    job never runs again. A bare `defer-{job_id}` works once and is then
    deduped from the second round onward — the same bug, one round later. The
    counter is what makes each round distinct while still collapsing a
    duplicate delivery of the SAME round.
    """
    from stylist_worker.state_machine import deferral_count

    job_id, user_id = await _make_job(owner_engine)
    try:
        assert await deferral_count(user_id, job_id) == 0
        ids = set()
        for expected in range(3):
            count = await deferral_count(user_id, job_id)
            assert count == expected
            ids.add(f"defer-{job_id}-{count}")
            await _bump_deferral(owner_engine, job_id)
        assert len(ids) == 3, "deferral rounds collided on one arq job id"
    finally:
        await _cleanup(owner_engine, user_id)


async def test_a_short_blip_is_still_absorbed_in_process(owner_engine, make_stages) -> None:
    """The fast path must not regress. An ml pod finishing a model load is back
    within a second or two, and deferring that costs a Redis round trip, a job
    dispatch and a row reload to avoid a 2s sleep — so short waits stay here.
    """
    job_id, user_id = await _make_job(owner_engine)
    matte = UnavailableStage("matte", unavailable_times=3, retry_after=0.01)
    stages, _ = make_stages(matte=matte)
    try:
        assert await run_pipeline(user_id, job_id, stages) == "complete"
        row = await _job_row(owner_engine, job_id)
        assert row["deferrals"] == 0, "a 10ms blip should never reach the queue"
        assert row["dependency_wait_s"] == 0
    finally:
        await _cleanup(owner_engine, user_id)


async def _bump_deferral(owner_engine, job_id: uuid.UUID) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with maker() as session:
        await session.execute(
            text("UPDATE jobs SET deferrals = deferrals + 1 WHERE id = :j"), {"j": job_id}
        )
        await session.commit()
