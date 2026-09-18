"""Persist dependency waits across re-enqueues, so a worker slot is not held.

THE BUG THIS EXISTS FOR
-----------------------
`Unavailable` — a dependency reporting "not up yet" — was handled by sleeping
IN-PROCESS for up to UNAVAILABLE_BUDGET_SECONDS (180s), which is correct about
the backpressure and wrong about where to wait. arq's concurrency is
WORKER_MAX_JOBS=2, so two jobs waiting on a down ml service occupied both slots
and the queue stopped draining for everyone. Known and unfixed since the
cross-phase pass on 2026-09-10; observed twice on 2026-09-17, once as an arq
job timeout (the 180s sleep outlived the job's own ceiling) and once as ten
parked jobs starving the queue.

The fix is to release the slot and re-enqueue with a delay. That only works if
the wait accumulated SO FAR survives the re-enqueue — otherwise every deferral
hands the next worker a fresh 180s budget and a permanently-down dependency
retries forever instead of reaching the DLQ. Which is the same class of bug as
`stage_attempts`: in-process retry state that resets when the process changes.

Both columns are on `jobs` rather than a new table because they are per-job
scalars read on the same row the state machine already loads.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_deferred_waits"
down_revision = "0013_export"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "dependency_wait_s",
            sa.Float(),
            nullable=False,
            server_default="0",
            comment="Cumulative seconds spent waiting on an unavailable dependency, "
            "across re-enqueues. Checked against UNAVAILABLE_BUDGET_SECONDS so a "
            "permanently-down dependency reaches the DLQ instead of deferring forever.",
        ),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "deferrals",
            sa.Integer(),
            nullable=False,
            server_default="0",
            # Also the arq dedupe key. A deferred re-enqueue reuses the job's
            # id plus this counter, so a duplicate delivery of the SAME
            # deferral round is deduped by arq while each new round enqueues —
            # reusing the relay's `outbox-{id}` would be silently dropped as
            # already-completed.
            comment="How many times this job has been deferred. Part of the arq "
            "_job_id for the re-enqueue, so each round is distinct and idempotent.",
        ),
    )


def downgrade() -> None:
    op.drop_column("jobs", "deferrals")
    op.drop_column("jobs", "dependency_wait_s")
