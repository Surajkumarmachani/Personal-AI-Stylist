"""Garment ingest and wardrobe listing.

Ingest writes the garment row, the job row AND the outbox row in ONE
transaction (§C1). Nothing is enqueued here: the relay (services/stylist_worker/
relay.py) picks the event up on its next tick and enqueues the pipeline job.

That indirection is the point. Enqueueing inline would mean two writes to two
systems with no atomicity between them — crash in the gap and the garment
exists with no processing, forever and silently.

WHY THIS HANDLER OPENS ITS OWN TRANSACTION INSTEAD OF TAKING `TenantDB`
-----------------------------------------------------------------------
FastAPI runs the teardown of a `yield` dependency AFTER the response has been
sent. With the transaction owned by a dependency, the 202 reaches the client
BEFORE the COMMIT — so a client that does the obvious thing and immediately
polls `GET /jobs/{id}` with the id it was just handed gets a 404. Read-your-own-
writes, violated by dependency ordering rather than by anything in the query.

It is a race, so it hides: a client that waits a few hundred milliseconds (or a
test that sleeps) never sees it, and it surfaces later as a rare, unreproducible
"job not found" on a fast network. Writes therefore manage their own
transaction inline, and it is committed before this function returns.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Response, status
from sqlalchemy import select, text

from stylist_api.deps import CurrentUser, ObjectStoreDep, QueueRedisDep, TenantDB
from stylist_api.schemas import GarmentSummary, IngestRequest, IngestResponse
from stylist_db.models import Garment, Job
from stylist_db.outbox import emit
from stylist_db.session import tenant_session

router = APIRouter(tags=["wardrobe"])


@router.post(
    "/garments/ingest",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest(
    body: IngestRequest,
    user: CurrentUser,
    redis: QueueRedisDep,
    store: ObjectStoreDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> IngestResponse:
    if len(body.upload_ids) != len(body.keys):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="upload_ids and keys must be the same length",
        )
    if idempotency_key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )

    # A presign handed out is not evidence that bytes exist. Confirm each object
    # actually landed before creating work for it.
    for key in body.keys:
        if not key.startswith(f"originals/{user.id}/"):
            # Defence against a client passing another tenant's key.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="key does not belong to caller"
            )
        if store.head(key) is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"no uploaded object at {key}",
            )

    # Job ids are minted UP FRONT, before anything is written, so the whole
    # batch can be claimed under one Idempotency-Key. Claiming after insert
    # would leave a window where a retry starts a second pipeline.
    job_ids = [uuid.uuid4() for _ in body.upload_ids]

    claim = await redis.claim_idempotency_key(user_id=user.id, key=idempotency_key, job_ids=job_ids)
    if not claim.created:
        # A replay. Return the ORIGINAL ids, in order — the client pairs them
        # with the uploads it sent, so a reordered or truncated list attaches
        # progress streams to the wrong photos.
        response.status_code = status.HTTP_200_OK
        return IngestResponse(
            job_ids=list(claim.job_ids),
            accepted=len(claim.job_ids),
            idempotent_replay=True,
        )

    # ONE transaction for the whole batch: garments + jobs + outbox. If this
    # crashes anywhere, none of it happened — no orphan garment with no
    # processing, no job referencing a row that does not exist.
    #
    # Opened inline (not via the TenantDB dependency) so it COMMITS BEFORE this
    # function returns — see the module docstring. A client is handed job ids it
    # can immediately read.
    async with tenant_session(user.id) as db:
        for index, (upload_id, key) in enumerate(zip(body.upload_ids, body.keys, strict=True)):
            garment_id = uuid.uuid4()
            job_id = job_ids[index]

            db.add(
                Garment(
                    id=garment_id,
                    user_id=user.id,
                    original_key=key,
                    state="received",
                )
            )
            db.add(
                Job(
                    id=job_id,
                    user_id=user.id,
                    kind="ingest",
                    state="received",
                    garment_id=garment_id,
                    # Only the first row carries the bare key. The DB's
                    # UNIQUE (user_id, idempotency_key) is the durable backstop
                    # if Redis loses the claim; the suffix keeps the rest of the
                    # batch from colliding with it.
                    idempotency_key=(
                        idempotency_key if index == 0 else f"{idempotency_key}:{index}"
                    ),
                    payload={"upload_id": upload_id, "key": key},
                )
            )
            # Same transaction as the rows above. emit() refuses to run outside
            # one, so this cannot silently become a separate write.
            await emit(
                db,
                aggregate_id=garment_id,
                user_id=user.id,
                event_type="garment.ingested",
                payload={"job_id": str(job_id), "garment_id": str(garment_id), "key": key},
            )

    return IngestResponse(job_ids=job_ids, accepted=len(job_ids))


@router.get("/garments", response_model=list[GarmentSummary])
async def list_garments(
    user: CurrentUser,
    db: TenantDB,
    store: ObjectStoreDep,
    limit: Annotated[int, Query(le=200, ge=1)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[GarmentSummary]:
    """Note the absence of `.where(Garment.user_id == user.id)`.

    That is deliberate and it is the whole point of RLS: the policy scopes this
    query to the caller's tenant at the database, so forgetting the filter is
    not a data leak. tests/test_rls_isolation.py asserts exactly this.
    """
    # `rejected` and `quarantined` are hidden, matching search.py and
    # evalview.py. A photo the pipeline refused is not a garment, and leaving
    # it here showed the user a blank card with no slot and no explanation —
    # which is worse than showing nothing, because it looks like a garment
    # that failed to load. `needs_review` and `duplicate_suspect` stay VISIBLE:
    # those rows exist and need the user's attention.
    rows = await db.execute(
        select(Garment)
        .where(Garment.is_active.is_(True))
        .where(Garment.state.notin_(("rejected", "quarantined")))
        .order_by(Garment.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    garments = list(rows.scalars())

    # WEAR COUNTS IN ONE QUERY, not one per garment. A page of 50 would
    # otherwise be 50 round trips for a number shown on a card.
    by_id: dict[str, tuple[int, Any]] = {}
    if garments:
        # Skipped entirely on an empty page: `= ANY('{}')` is valid but the
        # round trip is not, and an empty wardrobe is the common first request.
        wears = await db.execute(
            text(
                "SELECT garment_id, count(*) AS n, max(worn_on) AS last_worn "
                "FROM wear_log WHERE garment_id = ANY(CAST(:ids AS uuid[])) GROUP BY garment_id"
            ),
            {"ids": [str(g.id) for g in garments]},
        )
        by_id = {
            str(r["garment_id"]): (int(r["n"] or 0), r["last_worn"]) for r in wears.mappings()
        }

    return [
        GarmentSummary(
            id=g.id,
            slot=g.slot,
            subcategory=g.subcategory,
            primary_colour=g.primary_colour,
            state=g.state,
            needs_review=g.needs_review,
            needs_wash=bool(g.needs_wash),
            brand=g.brand,
            size_label=g.size_label,
            wear_count=by_id.get(str(g.id), (0, None))[0],
            last_worn=by_id.get(str(g.id), (0, None))[1],
            cutout_url=store.presign_download(g.cutout_key) if g.cutout_key else None,
            created_at=g.created_at,
        )
        for g in garments
    ]


@router.get("/garments/{garment_id}/similar", response_model=list[GarmentSummary])
async def similar_garments(
    garment_id: uuid.UUID,
    user: CurrentUser,
    db: TenantDB,
    store: ObjectStoreDep,
    limit: Annotated[int, Query(le=50, ge=1)] = 10,
) -> list[GarmentSummary]:
    """Garments closest to this one in embedding space (Step 3.4).

    WHY THIS DOES NOT USE THE HNSW INDEX, AND THAT IS CORRECT
    ---------------------------------------------------------
    The query is scoped to one tenant by RLS, so the candidate set is at most a
    few hundred rows. Postgres will choose a sequential scan with exact cosine
    distances over an approximate index, and that is the better plan: perfect
    recall, no ef_search tuning, and faster at this size.

    The HNSW index from migration 0002 exists for CROSS-tenant style similarity
    in Phase 11, where the candidate set is millions of rows. §B2 spells this
    out because both misreadings are tempting: dropping an index that looks
    unused, or assuming this endpoint is what it serves.

    `<=>` is pgvector's cosine distance: 0 is identical, 2 is opposite. Vectors
    are stored L2-normalised, so it is a true cosine.
    """
    anchor = await db.execute(
        select(Garment).where(Garment.id == garment_id, Garment.is_active.is_(True))
    )
    garment = anchor.scalar_one_or_none()
    if garment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="garment not found")
    if garment.embedding is None:
        # Not an error: the garment is real but has not reached the embed
        # stage. 409 rather than 404 so the client can retry instead of
        # concluding the garment does not exist.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="garment has no embedding yet; still processing",
        )

    # No user_id filter: RLS scopes it, exactly as everywhere else. That is
    # what stops a similarity query becoming a cross-tenant read.
    rows = await db.execute(
        text(
            """
            SELECT id, slot, subcategory, primary_colour, state, needs_review,
                   cutout_key, created_at,
                   embedding <=> (SELECT embedding FROM garments WHERE id = :gid)
                     AS distance
            FROM garments
            WHERE is_active
              AND embedding IS NOT NULL
              AND id <> :gid
            ORDER BY distance
            LIMIT :limit
            """
        ),
        {"gid": garment_id, "limit": limit},
    )
    return [
        GarmentSummary(
            id=row.id,
            slot=row.slot,
            subcategory=row.subcategory,
            primary_colour=row.primary_colour,
            state=row.state,
            needs_review=row.needs_review,
            cutout_url=store.presign_download(row.cutout_key) if row.cutout_key else None,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.delete("/garments/{garment_id}", status_code=status.HTTP_200_OK)
async def remove_garment(
    garment_id: uuid.UUID,
    user: CurrentUser,
    db: TenantDB,
) -> dict[str, object]:
    """Take a garment out of the wardrobe.

    SOFT DELETE, for the reason duplicates.py already gives about merging:
    `is_active = false` rather than DELETE, because the cutout, the tags and
    the embedding took real work and a mis-tap should cost an undo rather than
    a re-upload. `is_active` is the predicate every read already applies — the
    wardrobe grid, search, the candidate pool, the eval view — so one column
    removes it from all of them at once, with no second notion of "gone".

    THE WEAR LOG STAYS. A garment worn eleven times WAS worn eleven times, and
    cost-per-wear and the style vector are both built by replaying that log.
    Deleting the history to tidy the grid would silently rewrite numbers the
    user has already seen, which is a worse outcome than a retired row.

    WHAT THIS DOES NOT DO: it does not delete the photograph. The original and
    the cutout stay in object storage, and the response says so rather than
    implying an erasure it did not perform. Deleting a user's pixels is
    `DELETE /me`, which is audited, staged and covered by the retention SLA —
    quietly doing a fraction of that here would make the strong guarantee
    harder to reason about, not easier.

    Idempotent: removing an already-removed garment is a 200 with
    `already_removed`, not a 404. The client that retries after a dropped
    response is right to, and punishing it would be the only effect.
    """
    row = await db.execute(
        # No user_id predicate: RLS scopes it, so another tenant's id is a 404
        # rather than a 403 and we do not confirm the id exists.
        text("SELECT id, is_active, subcategory::text AS subcategory FROM garments WHERE id = :g"),
        {"g": garment_id},
    )
    garment = row.mappings().one_or_none()
    if garment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="garment not found")
    if not garment["is_active"]:
        return {
            "garment_id": str(garment_id),
            "removed": True,
            "already_removed": True,
            "photo_retained": True,
        }

    await db.execute(
        text("UPDATE garments SET is_active = false, updated_at = now() WHERE id = :g"),
        {"g": garment_id},
    )
    # Same transaction as the update. Downstream cares: the outfits table can
    # hold precomputed sets naming this garment, and they have to be pruned or
    # the next suggestion hydrates a garment the user has removed.
    await emit(
        db,
        aggregate_id=garment_id,
        user_id=user.id,
        event_type="garment.removed",
        payload={"garment_id": str(garment_id), "subcategory": garment["subcategory"]},
    )

    # Prune precomputed outfits that name it, in the SAME transaction.
    #
    # Otherwise the removed garment comes straight back: `GET /suggestions`
    # serves from `outfits` first, `_hydrate` drops the inactive garment, and
    # the outfit is rendered a piece short with no explanation. The live path
    # would regenerate correctly — but only once the materialised rows are
    # gone, which is what this does.
    pruned = await db.execute(
        text(
            "DELETE FROM outfits WHERE :g = ANY(garment_ids) RETURNING id",
        ),
        {"g": garment_id},
    )
    return {
        "garment_id": str(garment_id),
        "removed": True,
        "already_removed": False,
        "outfits_pruned": len(pruned.fetchall()),
        # Stated, not implied. See the docstring.
        "photo_retained": True,
    }
