"""Transactional outbox helpers (§C1).

THE RULE THIS FILE EXISTS TO ENFORCE
------------------------------------
`INSERT garment` and `redis.enqueue(job)` are two operations against two
systems. Crash between them and either the garment exists with no processing
forever and silently, or the worker picks up a job for a row that is not there.
No amount of retry logic fixes it, because the intent was never durably
recorded anywhere.

So the intent is recorded in Postgres, in the SAME transaction as the state
change it describes, and a separate relay moves it to the queue. `emit()` takes
a session rather than opening one precisely so it cannot be called outside the
caller's transaction by accident.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from stylist_db.models import Outbox


async def emit(
    session: AsyncSession,
    *,
    aggregate_id: uuid.UUID,
    user_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
) -> uuid.UUID:
    """Record an event inside the caller's open transaction.

    MUST be called inside a transaction that also performs the state change.
    Committing the event without the change (or vice versa) is the exact bug
    the outbox exists to prevent, so never open a session in here.
    """
    if not session.in_transaction():
        raise RuntimeError(
            "outbox.emit() called outside a transaction — the event must commit "
            "atomically with the state change it describes"
        )
    event_id = uuid.uuid4()
    session.add(
        Outbox(
            id=event_id,
            aggregate_id=aggregate_id,
            user_id=user_id,
            event_type=event_type,
            payload=payload,
        )
    )
    return event_id


# Claims a batch of unsent events for this relay replica.
#
# FOR UPDATE SKIP LOCKED is what lets N replicas run with zero coordination:
# each takes rows nobody else has locked, and a crashed replica's locks are
# released by Postgres on connection loss, so its rows are simply re-claimed on
# the next tick. No leases, no heartbeats, no split-brain.
#
# ORDER BY created_at keeps delivery roughly causal. It is not a total order
# guarantee across replicas, and consumers must not depend on one — they are
# idempotent instead (§C3).
CLAIM_BATCH_SQL = text(
    """
    SELECT id, aggregate_id, user_id, event_type, payload
    FROM outbox
    WHERE sent_at IS NULL
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT :limit
    """
)

MARK_SENT_SQL = text("UPDATE outbox SET sent_at = now() WHERE id = ANY(:ids)")


async def unsent_depth(session: AsyncSession) -> int:
    """Relay lag, in events. An SLI: a growing number means the relay is down
    or wedged, and ingests are silently not starting."""
    row = await session.execute(text("SELECT count(*) FROM outbox WHERE sent_at IS NULL"))
    return int(row.scalar_one())
