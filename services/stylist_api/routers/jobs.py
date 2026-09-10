"""Job status and the SSE progress stream (Step 2.4)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from stylist_api.deps import CurrentUser, TenantDB
from stylist_api.schemas import JobStatus
from stylist_api.settings import get_settings
from stylist_db.models import Job
from stylist_db.session import tenant_session

router = APIRouter(prefix="/jobs", tags=["jobs"])

# Polling, not pub/sub, and that is a deliberate Phase 2 choice. A Redis
# pub/sub fan-out would avoid the query, but it also means a client that
# connects a moment late misses transitions that already happened and then
# waits forever. Reading the row is authoritative: whenever the client
# connects, it immediately gets the CURRENT state and every subsequent change.
# At a 10s ingest budget and a handful of concurrent uploads per user, one
# indexed primary-key read per 500ms is not a load problem worth optimising
# before it shows up as one.
#
# Interval and cap come from settings, not constants — see settings.py.

TERMINAL_JOB_STATES = frozenset(
    {"complete", "rejected", "quarantined", "needs_review", "duplicate_suspect"}
)


@router.get("/{job_id}", response_model=JobStatus)
async def get_job(job_id: uuid.UUID, user: CurrentUser, db: TenantDB) -> JobStatus:
    # No user_id filter: RLS scopes it. Another tenant's job id therefore
    # returns 404 rather than 403 — we do not confirm that the id exists.
    row = await db.execute(select(Job).where(Job.id == job_id))
    job = row.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return JobStatus(
        id=job.id,
        kind=job.kind,
        state=job.state,
        garment_id=job.garment_id,
        last_error=job.last_error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


async def _job_snapshot(user_id: uuid.UUID, job_id: uuid.UUID) -> dict[str, object] | None:
    """One tenant-scoped read. Opens and closes its own transaction per poll so
    the stream does not hold one open for its whole lifetime — a long-lived
    idle transaction blocks vacuum and inflates bloat."""
    async with tenant_session(user_id) as session:
        row = await session.execute(select(Job).where(Job.id == job_id))
        job = row.scalar_one_or_none()
        if job is None:
            return None
        return {
            "job_id": str(job.id),
            "state": job.state,
            "garment_id": str(job.garment_id) if job.garment_id else None,
            "last_error": job.last_error,
            "dlq": job.dlq_at is not None,
            "updated_at": job.updated_at.isoformat(),
        }


def _sse(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.get("/{job_id}/events")
async def job_events(job_id: uuid.UUID, user: CurrentUser, request: Request) -> StreamingResponse:
    """Server-sent events for one job's state transitions.

    Emits the current state immediately on connect, then one event per change,
    then closes on a terminal state (or DLQ, or the stream cap).

    SSE rather than WebSocket: this is one-directional, it survives proxies
    that mangle WS upgrades, and the browser reconnects on its own. There is
    nothing to send upstream, so a duplex protocol would be strictly more
    moving parts for no capability.
    """
    settings = get_settings()
    poll_interval = settings.sse_poll_interval_seconds
    max_seconds = settings.sse_max_stream_seconds

    first = await _job_snapshot(user.id, job_id)
    if first is None:
        # Checked before opening the stream so an unknown job is a clean 404
        # rather than a 200 that streams an error the client has to parse.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")

    async def stream() -> AsyncIterator[str]:
        last_serialised: str | None = None
        elapsed = 0.0

        snapshot = first
        while True:
            if await request.is_disconnected():
                break

            serialised = json.dumps(snapshot, sort_keys=True)
            if serialised != last_serialised:
                yield _sse("state", snapshot)
                last_serialised = serialised

            state = str(snapshot.get("state"))
            if state in TERMINAL_JOB_STATES or snapshot.get("dlq"):
                yield _sse("done", {"job_id": str(job_id), "state": state})
                break

            if elapsed >= max_seconds:
                # Say so explicitly. A stream that just stops is
                # indistinguishable from a network failure, and the client
                # should reconnect rather than assume the job died.
                yield _sse("timeout", {"job_id": str(job_id), "state": state})
                break

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            refreshed = await _job_snapshot(user.id, job_id)
            if refreshed is None:
                yield _sse("gone", {"job_id": str(job_id)})
                break
            snapshot = refreshed

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Tells nginx not to buffer, which would otherwise hold events
            # until the response completed and defeat the whole point.
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
