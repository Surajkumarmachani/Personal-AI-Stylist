"""Outbox relay: Postgres -> job queue.

Runs as its own arq cron task on a short tick. Reads unsent outbox rows with
FOR UPDATE SKIP LOCKED, enqueues the corresponding job, then marks the rows
sent — in that order, deliberately.

WHY ENQUEUE BEFORE MARKING SENT
-------------------------------
The two operations still span two systems, so one of them has to go first and
the choice decides which failure you get:

  mark sent, then enqueue  ->  crash between them LOSES the work, permanently
                               and silently. The row says delivered; nothing
                               ever ran. This is the bug the outbox exists to
                               prevent, reintroduced one layer down.
  enqueue, then mark sent  ->  crash between them DUPLICATES the enqueue. The
                               row is still unsent, so the next tick enqueues
                               again.

At-least-once is the correct trade because duplicates are cheap to make
harmless and lost work is not. Two mechanisms absorb them: the arq job id is
derived from the outbox row id, so a re-enqueue of the same event is refused by
the queue itself, and consumers additionally guard with `processed_keys` (§C3).
That combination is what makes at-least-once delivery behave as
effectively-exactly-once.
"""

from __future__ import annotations

import logging
from typing import Any

from arq import ArqRedis

from stylist_db.outbox import CLAIM_BATCH_SQL, MARK_SENT_SQL
from stylist_db.session import system_session

logger = logging.getLogger(__name__)

BATCH_LIMIT = 100

# outbox event_type -> the arq function that handles it.
EVENT_HANDLERS: dict[str, str] = {
    "garment.ingested": "ingest_photo",
    # An export is queued through the OUTBOX rather than enqueued directly by
    # the API, so the request that asked for it and the job that does it share
    # one transaction. A direct enqueue can succeed and then have its
    # transaction roll back, leaving a job for a row that does not exist — the
    # exact failure the outbox exists to prevent.
    "export.requested": "build_export",
    # Phase 6+ will add: feedback.recorded -> invalidate_precompute.
}


async def relay_outbox(ctx: dict[str, Any]) -> dict[str, int]:
    """One tick. Returns counts so the arq result is useful in logs.

    Runs with NO tenant context (system_session): the relay spans every tenant
    by definition, and outbox is deliberately not an RLS-scoped table.
    """
    redis: ArqRedis = ctx["redis"]
    claimed = 0
    enqueued = 0
    skipped_duplicate = 0
    unroutable = 0

    async with system_session() as session:
        rows = (await session.execute(CLAIM_BATCH_SQL, {"limit": BATCH_LIMIT})).mappings().all()
        claimed = len(rows)
        if not rows:
            return {"claimed": 0, "enqueued": 0, "duplicate": 0, "unroutable": 0}

        sent_ids = []
        for row in rows:
            handler = EVENT_HANDLERS.get(row["event_type"])
            if handler is None:
                # Mark it sent anyway: an event nobody handles is not an error
                # that should wedge the relay behind an ever-growing backlog.
                # It is logged so an unrouted event type is visible.
                unroutable += 1
                logger.warning(
                    "outbox event has no handler; marking sent",
                    extra={"event_type": row["event_type"], "outbox_id": str(row["id"])},
                )
                sent_ids.append(row["id"])
                continue

            # Job id derived from the outbox row id. arq refuses a duplicate
            # job id, so a re-delivered event cannot start a second pipeline.
            #
            # `_job_id`, with the underscore. arq reserves underscore-prefixed
            # kwargs for itself and forwards everything else to the task, so
            # `job_id=` here does not dedupe — it gets passed to ingest_photo
            # as an argument it does not accept, and every single ingest dies
            # with a TypeError. The dedupe silently does not happen either.
            job = await redis.enqueue_job(
                handler,
                _job_id=f"outbox-{row['id']}",
                user_id=str(row["user_id"]),
                aggregate_id=str(row["aggregate_id"]),
                payload=dict(row["payload"]),
            )
            if job is None:
                # Already queued or completed under this id — the expected
                # outcome after a crash between enqueue and mark-sent.
                skipped_duplicate += 1
            else:
                enqueued += 1
            sent_ids.append(row["id"])

        # Same transaction that holds the row locks, so the mark and the
        # release happen together.
        await session.execute(MARK_SENT_SQL, {"ids": sent_ids})

    if claimed:
        logger.info(
            "relay tick: claimed=%d enqueued=%d duplicate=%d unroutable=%d",
            claimed,
            enqueued,
            skipped_duplicate,
            unroutable,
        )
    return {
        "claimed": claimed,
        "enqueued": enqueued,
        "duplicate": skipped_duplicate,
        "unroutable": unroutable,
    }
