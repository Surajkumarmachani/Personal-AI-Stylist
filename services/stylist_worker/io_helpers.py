"""Fetching stage inputs in a way that survives a resume.

Every helper here follows the same shape: use `ctx.scratch` if the previous
stage in THIS run left the bytes there, otherwise fetch them from the object
store by a derived key. That is what makes a stage runnable on a fresh worker
that has no memory of the earlier stages.
"""

from __future__ import annotations

from typing import Any

from stylist_worker.keys import sanitised_key
from stylist_worker.state_machine import IngestState, JobContext, Terminal


def load_sanitised(ctx: JobContext, store: Any) -> bytes:
    """The EXIF-stripped whole frame.

    Falls back to the object store rather than trusting scratch, because on a
    resume the sanitise stage was skipped and scratch is empty. Getting this
    wrong is not a subtle degradation — it is a KeyError on every resumed job.
    """
    cached = ctx.scratch.get("sanitised_bytes")
    if cached:
        return bytes(cached)

    key = ctx.scratch.get("sanitised_key") or sanitised_key(ctx.user_id, ctx.job_id)
    if store.head(key) is None:
        # The intermediate is gone but the job says it was produced. Rebuilding
        # it means re-running sanitise, which the state machine will not do —
        # so this is terminal rather than a silent half-result.
        raise Terminal(
            IngestState.REJECTED,
            f"sanitised intermediate missing at {key}; re-ingest the photo",
        )
    data = store.get_bytes(key)
    ctx.scratch["sanitised_bytes"] = data
    return bytes(data)


def load_cutout(ctx: JobContext, store: Any, key: str) -> bytes:
    if store.head(key) is None:
        raise Terminal(IngestState.REJECTED, f"cutout missing at {key}")
    return bytes(store.get_bytes(key))
