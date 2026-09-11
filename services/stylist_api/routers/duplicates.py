"""Resolving a proposed duplicate (Step 5.1, the half that involves the user).

The dedupe stage never merges — it parks a garment at DUPLICATE_SUSPECT and
points `duplicate_of` at what it thinks the garment already is. This is where
that question gets answered, and there are exactly two answers:

  "they are different"  -> clear the proposal, return the garment to the
                           wardrobe. The pipeline was wrong; nothing is lost.
  "they are the same"   -> deactivate the NEW garment and move its wear log
                           onto the one that was already there.

MERGING IS A SOFT DELETE, AND THE WEAR LOG MOVES
------------------------------------------------
`is_active = false` rather than DELETE, because the cutout and the embedding
took real work and a user who merges by mistake should get an undo rather than
a re-upload. And the wear rows are reparented instead of dropped: a user who
logged three wearings against the duplicate before noticing did wear the
garment three times, and cost-per-wear must not quietly lose them.

Conflicts are possible — both garments worn on the same day — so the move is
ON CONFLICT DO NOTHING against the one-wearing-per-day index. The union of the
dates is the truthful answer, not the sum.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text

from stylist_api.deps import CurrentUser, TenantDB
from stylist_db.outbox import emit
from stylist_db.session import tenant_session

router = APIRouter(tags=["duplicates"])


class ResolutionRequest(BaseModel):
    # No default. Merging and keeping are opposite and irreversible-ish
    # actions, and a default here would let a malformed client pick one.
    resolution: Literal["different", "same"]


@router.get("/wardrobe/duplicates")
async def pending_duplicates(user: CurrentUser, db: TenantDB) -> dict[str, Any]:
    """Everything waiting on an answer, with both sides of each comparison."""
    rows = await db.execute(
        text(
            """
            SELECT g.id, g.subcategory::text AS subcategory,
                   g.primary_colour::text AS primary_colour, g.cutout_key, g.phash,
                   o.id AS other_id, o.subcategory::text AS other_subcategory,
                   o.primary_colour::text AS other_primary_colour,
                   o.cutout_key AS other_cutout_key,
                   o.created_at AS other_created_at, g.created_at
            FROM garments g
            JOIN garments o ON o.id = g.duplicate_of
            WHERE g.is_active AND g.state = 'duplicate_suspect'
            ORDER BY g.created_at DESC
            """
        )
    )
    return {
        "items": [
            {
                "garment_id": str(r["id"]),
                "subcategory": r["subcategory"],
                "primary_colour": r["primary_colour"],
                "created_at": r["created_at"].isoformat(),
                "duplicate_of": {
                    "id": str(r["other_id"]),
                    "subcategory": r["other_subcategory"],
                    "primary_colour": r["other_primary_colour"],
                    "created_at": r["other_created_at"].isoformat(),
                },
            }
            for r in rows.mappings()
        ]
    }


@router.post("/garments/{garment_id}/duplicate-resolution")
async def resolve_duplicate(
    garment_id: uuid.UUID, body: ResolutionRequest, user: CurrentUser
) -> dict[str, Any]:
    # Own transaction, not TenantDB: a merge moves wear rows and retires a
    # garment, and the client refreshes the wardrobe the moment it gets 200.
    # With the COMMIT in dependency teardown the refresh can still show the
    # merged garment. See wear.py for the full reasoning.
    async with tenant_session(user.id) as db:
        return await _resolve(db, garment_id, body, user)


async def _resolve(
    db: Any, garment_id: uuid.UUID, body: ResolutionRequest, user: Any
) -> dict[str, Any]:
    row = await db.execute(
        text(
            "SELECT id, duplicate_of, state FROM garments "
            "WHERE id = :gid AND is_active AND state = 'duplicate_suspect'"
        ),
        {"gid": garment_id},
    )
    garment = row.mappings().one_or_none()
    if garment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no unresolved duplicate proposal for this garment",
        )
    original_id = garment["duplicate_of"]

    if body.resolution == "different":
        await db.execute(
            text(
                """
                UPDATE garments
                SET duplicate_of = NULL, state = 'matted', updated_at = now()
                WHERE id = :gid
                """
            ),
            {"gid": garment_id},
        )
        # Recorded because a proposal the user rejects is the signal that the
        # thresholds are too loose. Without it, tuning them is guesswork.
        await emit(
            db,
            aggregate_id=garment_id,
            user_id=user.id,
            event_type="garment.duplicate_rejected",
            payload={"garment_id": str(garment_id), "proposed": str(original_id)},
        )
        return {"garment_id": str(garment_id), "resolution": "different", "merged_into": None}

    # "same": move the wear history, then retire the duplicate.
    moved = await db.execute(
        text(
            """
            INSERT INTO wear_log (id, user_id, garment_id, worn_on, note)
            SELECT gen_random_uuid(), user_id, :orig, worn_on, note
            FROM wear_log WHERE garment_id = :gid
            ON CONFLICT (garment_id, worn_on) DO NOTHING
            RETURNING id
            """
        ),
        {"orig": original_id, "gid": garment_id},
    )
    moved_count = len(list(moved))
    await db.execute(text("DELETE FROM wear_log WHERE garment_id = :gid"), {"gid": garment_id})
    await db.execute(
        text(
            """
            UPDATE garments
            SET is_active = false, state = 'duplicate_suspect', updated_at = now()
            WHERE id = :gid
            """
        ),
        {"gid": garment_id},
    )
    await emit(
        db,
        aggregate_id=garment_id,
        user_id=user.id,
        event_type="garment.merged",
        payload={
            "garment_id": str(garment_id),
            "merged_into": str(original_id),
            "wears_moved": moved_count,
        },
    )
    return {
        "garment_id": str(garment_id),
        "resolution": "same",
        "merged_into": str(original_id),
        "wears_moved": moved_count,
    }
