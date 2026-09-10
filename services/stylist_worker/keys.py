"""Deterministic object-store keys.

WHY THESE ARE DERIVED RATHER THAN REMEMBERED
--------------------------------------------
Stages pass results to each other through `ctx.scratch`, which is in-memory and
therefore gone the moment a worker dies. A stage that reads
`ctx.scratch["sanitised_bytes"]` works perfectly in a single run and crashes on
every RESUME, because the stage that populated it was skipped as already-done.

That was a real bug: resuming a job parked at MODERATED raised
`KeyError('sanitised_key')`. It went unnoticed because the resume test used
fake stages, so it exercised the state machine's control flow and never the
stages' actual data dependencies.

Deriving every key from ids the job already carries means any stage can fetch
what it needs from the object store at any time, on any worker, with no
handover. Scratch becomes a cache — nice when it is warm, never required.

Keys are tenant-prefixed so the erasure saga can delete a user's objects by
prefix rather than by table scan (§C5 step 4).
"""

from __future__ import annotations

import uuid


def original_key(user_id: uuid.UUID | str, upload_id: uuid.UUID | str) -> str:
    """Where the client uploaded to. Issued by the presign endpoint."""
    return f"originals/{user_id}/{upload_id}"


def sanitised_key(user_id: uuid.UUID | str, job_id: uuid.UUID | str) -> str:
    """EXIF-stripped, orientation-baked intermediate. One per JOB, because it
    is the whole frame before any garment split."""
    return f"sanitised/{user_id}/{job_id}.png"


def cutout_key(user_id: uuid.UUID | str, garment_id: uuid.UUID | str) -> str:
    """Per GARMENT, not per job.

    One photo can yield several garments after segmentation, so a job-keyed
    cutout would have them overwrite each other — the second garment silently
    replacing the first's image.
    """
    return f"cutouts/{user_id}/{garment_id}.png"


def mask_key(user_id: uuid.UUID | str, garment_id: uuid.UUID | str) -> str:
    """The segmentation mask a garment came from.

    Kept so the NEEDS_REVIEW crop UI can show what the model thought, and so a
    re-matte after a model upgrade does not need to re-segment.
    """
    return f"masks/{user_id}/{garment_id}.png"
