"""Erasure, body-photo revocation, and export (§C5, Phase 9).

THREE SEPARATE RIGHTS, THREE SEPARATE ENDPOINTS
------------------------------------------------
  DELETE /me              erase the account (the 7-step saga)
  DELETE /me/body-photos  revoke try-on consent WITHOUT deleting the account
  GET    /me/export       portability: everything we hold, as a file

§C5 requires the middle one explicitly: "users must be able to revoke that
consent without deleting their account". Bundling it into account deletion
would make withdrawing consent for ONE feature cost you the whole product,
which is the kind of choice that makes consent meaningless.

DELETE /me RETURNS IMMEDIATELY, AND THE ACCOUNT IS ALREADY DEAD
----------------------------------------------------------------
Step 1 runs synchronously: `deleted_at` is set and every auth path starts
returning 410 before this handler responds. Steps 2-7 take as long as they
take, against a 30-day SLA. A user who taps "delete my account" and can still
log in a second later does not believe anything else we say about deletion.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from stylist_api.deps import CurrentUser, ObjectStoreDep, TenantDB
from stylist_db.outbox import emit
from stylist_db.session import tenant_session

router = APIRouter(tags=["privacy"])

# Well under the 7-day life of the archive itself. A link that outlived the
# object would 404; one that matched it exactly would keep working right up to
# the moment the sweep ran, which is a worse experience than a fresh link on
# every check.
EXPORT_URL_TTL_SECONDS = 6 * 3600

# The table list lives in `stylist_worker.export`, next to the code that reads
# it. Two copies would drift, and the one that matters is the one that runs.


@router.delete("/me", status_code=status.HTTP_202_ACCEPTED)
async def delete_account(
    user: CurrentUser,
    confirm: Annotated[str, Query()] = "",
) -> dict[str, Any]:
    """Erase this account. Irreversible.

    `?confirm=DELETE` is required. Not a nicety: this endpoint is one stray
    fetch away from destroying everything a user owns, and unlike every other
    destructive action in this product there is no undo and no soft window the
    user controls.
    """
    if confirm != "DELETE":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="add ?confirm=DELETE to confirm; this erases your account and "
            "everything in it, and cannot be undone",
        )

    from stylist_worker.erasure import request_erasure

    result = await request_erasure(user.id, getattr(user, "email", None))
    return {
        **result,
        # The SLA is stated to the user, not just recorded. "We will delete
        # this" with no deadline is not an answer either regime accepts.
        "sla_days": 30,
        "note": "Your account is already disabled. Remaining data is purged in "
        "the background; some providers may retain copies for their own "
        "retention window, which will be listed when the erasure completes.",
    }


@router.delete("/me/body-photos", status_code=status.HTTP_200_OK)
async def revoke_body_photos(user: CurrentUser, store: ObjectStoreDep) -> dict[str, Any]:
    """Revoke try-on consent and delete the photos. Account untouched.

    §C5 steps 2, 4 and 5 only. Runs INLINE rather than as a saga because there
    is no ambiguity about what to delete and no dependency ordering — and
    because consent withdrawal that takes 30 days is not really withdrawal.

    ALL VERSIONS, like the account erasure: the bucket is versioned, so a plain
    delete leaves the body photo recoverable, which is the opposite of what the
    user just asked for.
    """
    async with tenant_session(user.id) as db:
        rows = await db.execute(
            text("SELECT id, object_key FROM body_photo WHERE revoked_at IS NULL")
        )
        photos = [dict(r) for r in rows.mappings()]
        if not photos:
            return {"revoked": 0, "deleted_objects": 0}

        deleted = 0
        for photo in photos:
            try:
                deleted += store.delete_all_versions(photo["object_key"])
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"could not delete stored photo: {type(exc).__name__}",
                ) from exc

        await db.execute(
            text(
                "UPDATE body_photo SET revoked_at = now(), deleted_from_storage = true "
                "WHERE revoked_at IS NULL"
            )
        )

    return {
        "revoked": len(photos),
        "deleted_objects": deleted,
        "account_deleted": False,
    }


@router.post("/me/export", status_code=status.HTTP_202_ACCEPTED)
async def request_export(user: CurrentUser) -> dict[str, Any]:
    """Start an export. §C5: async job -> signed ZIP -> 7-day link.

    This was synchronous and built the ZIP in memory. That works at nine
    garments and cannot work at two thousand: the archive holds every cutout
    the user owns, so it grows with the wardrobe while the request timeout does
    not — there is a size at which it silently starts failing and no obvious
    place to notice.

    POST rather than GET, now that it has a side effect: it enqueues work and
    writes a second copy of everything into storage, which is not something a
    link preview or a prefetch should be able to trigger.
    """
    export_id = uuid.uuid4()
    conflict = False

    async with tenant_session(user.id) as db:
        try:
            await db.execute(
                text("INSERT INTO export_request (id, user_id) VALUES (:i, :u)"),
                {"i": export_id, "u": user.id},
            )
            # INSIDE the same transaction as the INSERT. A direct enqueue could
            # succeed and then have this transaction roll back, leaving a job
            # for a row that does not exist; the outbox makes the two atomic.
            await emit(
                db,
                aggregate_id=export_id,
                user_id=user.id,
                event_type="export.requested",
                payload={},
            )
        except IntegrityError:
            # The partial unique index on (user_id) WHERE state IN
            # ('pending','building'), enforced in the DATABASE because a
            # double-tap on a slow connection is two concurrent requests and a
            # handler check races itself.
            conflict = True

    if conflict:
        # A SEPARATE SESSION, because the failed INSERT aborted the one above
        # and every subsequent statement in it raises
        # `InFailedSQLTransactionError`. Looking up the existing row inline
        # therefore produced a 500 for what is an ordinary, expected 409 —
        # measured against the running stack, and the third time this exact
        # trap has appeared in this codebase.
        async with tenant_session(user.id) as db:
            row = await db.execute(
                text(
                    "SELECT id, state FROM export_request "
                    "WHERE state IN ('pending', 'building') LIMIT 1"
                )
            )
            existing = row.mappings().one_or_none()
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"an export is already {existing['state']}; "
                f"check GET /me/export/{existing['id']}",
            )
        # The conflicting row completed between the INSERT and the lookup.
        # Retrying once is correct: the constraint that blocked us is gone.
        async with tenant_session(user.id) as db:
            await db.execute(
                text("INSERT INTO export_request (id, user_id) VALUES (:i, :u)"),
                {"i": export_id, "u": user.id},
            )
            await emit(
                db,
                aggregate_id=export_id,
                user_id=user.id,
                event_type="export.requested",
                payload={},
            )

    return {"export_id": str(export_id), "state": "pending", "expires_in_days": 7}


@router.get("/me/export/{export_id}")
async def export_status(
    export_id: uuid.UUID, user: CurrentUser, db: TenantDB, store: ObjectStoreDep
) -> dict[str, Any]:
    """Status, and a signed link once it is ready.

    The URL is minted per REQUEST rather than stored. A presigned URL is a
    bearer credential: storing one would mean a database row that grants access
    to the whole archive to anyone who can read the row, and it would outlive
    the 7-day window the record says it has.
    """
    row = await db.execute(
        text(
            "SELECT id, state, size_bytes, manifest, last_error, requested_at, "
            "completed_at, expires_at, deleted_at, object_key "
            "FROM export_request WHERE id = :i"
        ),
        {"i": export_id},
    )
    record = row.mappings().one_or_none()
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown export")

    body: dict[str, Any] = {k: v for k, v in dict(record).items() if k != "object_key"}
    body["id"] = str(record["id"])

    if record["deleted_at"] is not None:
        # Expired and swept. Said plainly rather than shown as a dead link:
        # the archive is gone on purpose, and the user can ask for another.
        body["download_url"] = None
        body["note"] = "this export has expired and been deleted; request a new one"
    elif record["state"] == "ready" and record["object_key"]:
        body["download_url"] = store.presign_download(
            record["object_key"], ttl_seconds=EXPORT_URL_TTL_SECONDS
        )
    else:
        body["download_url"] = None
    return body


@router.get("/me/erasure")
async def erasure_status(user: CurrentUser) -> dict[str, Any]:
    """Progress of an in-flight erasure, INCLUDING what could not be purged.

    Reachable while the account is already disabled, because the person most
    entitled to this answer is the one who asked for the deletion — and §C5
    requires the unpurgeable list to be DISCLOSED, which means somewhere a
    human can read it.
    """
    from stylist_db.session import system_session

    async with system_session() as db:
        row = await db.execute(
            text(
                "SELECT id, state, completed_steps, unpurgeable, counts, requested_at, "
                "completed_at, sla_deadline FROM erasure_request "
                "WHERE user_id = :u ORDER BY requested_at DESC LIMIT 1"
            ),
            {"u": user.id},
        )
        record = row.mappings().one_or_none()

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no erasure request for this account"
        )
    return {k: (str(v) if k == "id" else v) for k, v in dict(record).items()}
