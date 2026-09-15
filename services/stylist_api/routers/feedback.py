"""Outfit feedback, the style vector it feeds, and preference facts (Phase 8).

THE EVENT LOG IS THE PRODUCT; THE VECTOR IS A CACHE
---------------------------------------------------
Every write here APPENDS to `outfit_feedback` and then folds the event into
`user_style_vector`. The order matters and is not an implementation detail: the
event is durable first, so a crash between the two leaves a log that
`scripts/rebuild_style_vectors.py` can replay into the correct vector. The
reverse order would leave a vector nothing can justify.

The database enforces this rather than trusting these handlers — `stylist_app`
holds only SELECT and INSERT on `outfit_feedback` (migration 0009). A handler
that tried to "correct" a past event would get a permission error, which is the
point: the log is what every derived thing replays.

WRITE HANDLERS OWN THEIR TRANSACTION
------------------------------------
`tenant_session` inline, never the `TenantDB` dependency. FastAPI runs a
`yield` dependency's teardown AFTER the response is sent, so a handler taking
`TenantDB` returns 200 before its COMMIT lands and a client that taps "worn"
then re-reads sees the pre-write state. Phase 6 found this as a 1-in-4 flake
across five Phase 5 handlers; `wear.py` and `garments.py` carry the same note.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from stylist_api.deps import CurrentUser, TenantDB
from stylist_db.session import tenant_session
from stylist_domain.style import (
    DEFAULT_ALPHA,
    StyleVector,
    apply_event,
    outfit_embedding,
    parse_embedding,
)
from stylist_suggest import garment_set_hash

router = APIRouter(tags=["feedback"])

FeedbackKind = Literal["like", "dislike", "worn", "dismissed", "saved"]
FeedbackReason = Literal[
    "too_formal",
    "too_casual",
    "too_warm",
    "too_cold",
    "colours_clash",
    "not_my_style",
    "in_laundry",
    "wrong_occasion",
]
FactKind = Literal["avoids", "never", "prefers"]

# Fields a preference fact may be about. Restricted to what the candidate
# generator can actually FILTER on — a fact the pipeline cannot apply is a
# promise to the user that nothing keeps, and "legibility buys trust" only
# holds while what is shown is also what is enforced.
FACT_FIELDS = {"subcategory", "primary_colour", "material", "fit", "pattern"}


class FeedbackRequest(BaseModel):
    garment_ids: list[uuid.UUID] = Field(min_length=1, max_length=12)
    kind: FeedbackKind
    reason: FeedbackReason | None = None
    occasion: str | None = Field(default=None, max_length=32)
    # False when the user assembled this themselves. Wear-through rate is
    # worn-and-suggested over suggested, so conflating the two would inflate
    # the one metric Phase 8 calls "the only one that matters".
    was_suggested: bool = True


class FeedbackResponse(BaseModel):
    event_id: uuid.UUID
    kind: FeedbackKind
    style_vector_events: int
    style_vector_moved: bool


class FactRequest(BaseModel):
    kind: FactKind
    field_name: str = Field(max_length=32)
    field_value: str = Field(max_length=64)


async def _load_style(db: Any) -> StyleVector | None:
    row = await db.execute(
        text(
            "SELECT vector, events_applied, last_event_id::text AS last_event_id, alpha "
            "FROM user_style_vector LIMIT 1"
        )
    )
    r = row.mappings().one_or_none()
    if r is None:
        return None
    return StyleVector(
        vector=tuple(parse_embedding(r["vector"]) or ()),
        events_applied=int(r["events_applied"]),
        last_event_id=r["last_event_id"],
        alpha=float(r["alpha"]),
    )


async def _save_style(db: Any, user_id: uuid.UUID, style: StyleVector) -> None:
    await db.execute(
        text(
            """
            INSERT INTO user_style_vector
                (user_id, vector, events_applied, last_event_id, alpha, updated_at)
            VALUES (:uid, :vec, :n, :last, :alpha, now())
            ON CONFLICT (user_id) DO UPDATE SET
                vector = EXCLUDED.vector,
                events_applied = EXCLUDED.events_applied,
                last_event_id = EXCLUDED.last_event_id,
                alpha = EXCLUDED.alpha,
                updated_at = now()
            """
        ),
        {
            "uid": user_id,
            "vec": str(list(style.vector)),
            "n": style.events_applied,
            "last": style.last_event_id,
            "alpha": style.alpha,
        },
    )


@router.post(
    "/outfits/feedback",
    response_model=FeedbackResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_feedback(body: FeedbackRequest, user: CurrentUser) -> FeedbackResponse:
    """Append one feedback event and fold it into the style vector."""
    ids = [str(g) for g in body.garment_ids]

    async with tenant_session(user.id) as db:
        # Every garment must be ours and live. RLS already scopes the query, so
        # a garment belonging to someone else simply does not come back — this
        # is a 404 rather than a 403, and we do not confirm the id exists.
        rows = await db.execute(
            text(
                "SELECT id::text AS id, embedding FROM garments "
                "WHERE id = ANY(CAST(:ids AS uuid[])) AND is_active"
            ),
            {"ids": ids},
        )
        found = {r["id"]: r["embedding"] for r in rows.mappings()}
        missing = [g for g in ids if g not in found]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"unknown or inactive garments: {missing}",
            )

        event_id = uuid.uuid4()
        await db.execute(
            text(
                """
                INSERT INTO outfit_feedback
                    (id, user_id, garment_ids, garment_set_hash, occasion, kind,
                     reason, was_suggested)
                VALUES (:id, :uid, CAST(:ids AS uuid[]), :hash, :occasion,
                        CAST(:kind AS feedback_kind),
                        CAST(:reason AS feedback_reason), :suggested)
                """
            ),
            {
                "id": event_id,
                "uid": user.id,
                "ids": ids,
                "hash": garment_set_hash(ids),
                "occasion": body.occasion,
                "kind": body.kind,
                "reason": body.reason,
                "suggested": body.was_suggested,
            },
        )

        # The vector is folded AFTER the event is written, inside the same
        # transaction. An outfit whose garments have no embeddings yet moves
        # nothing — but the event is still recorded, so a later rebuild picks
        # it up once the embeddings exist.
        current = await _load_style(db)
        parsed = [parse_embedding(e) for e in found.values()]
        vec = outfit_embedding([e for e in parsed if e])
        moved = False
        if vec is not None:
            updated = apply_event(
                current,
                kind=body.kind,
                outfit_vec=vec,
                event_id=str(event_id),
                alpha=current.alpha if current else DEFAULT_ALPHA,
            )
            await _save_style(db, user.id, updated)
            moved = updated.vector != (current.vector if current else None)
            events = updated.events_applied
        else:
            events = current.events_applied if current else 0

        return FeedbackResponse(
            event_id=event_id,
            kind=body.kind,
            style_vector_events=events,
            style_vector_moved=moved,
        )


@router.get("/me/style")
async def get_style(user: CurrentUser, db: TenantDB) -> dict[str, Any]:
    """What the system believes about your taste, and how it got there.

    The vector itself is 768 floats and means nothing to a person, so this
    returns its PROVENANCE — how many events, which one last, what decay — and
    leaves the numbers out. A dashboard that dumps an embedding is not
    legibility, it is decoration.
    """
    style = await _load_style(db)
    counts = await db.execute(
        text("SELECT kind::text AS kind, count(*) AS n FROM outfit_feedback GROUP BY 1")
    )
    return {
        "events_applied": style.events_applied if style else 0,
        "last_event_id": style.last_event_id if style else None,
        "alpha": style.alpha if style else DEFAULT_ALPHA,
        "dimensions": len(style.vector) if style else 0,
        "feedback_by_kind": {r["kind"]: int(r["n"]) for r in counts.mappings()},
    }


@router.get("/me/wear-through")
async def wear_through(
    user: CurrentUser,
    db: TenantDB,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> dict[str, Any]:
    """Suggested -> actually worn. Phase 8 calls this the only quality metric
    that matters, so it is computed from the event log rather than stored.

    Denominator is DISTINCT suggested outfits, not events: a user who taps
    dislike three times on the same card has still only been suggested it once,
    and counting events would let indecision inflate the rate.
    """
    row = await db.execute(
        text(
            """
            SELECT
              count(DISTINCT garment_set_hash)
                FILTER (WHERE was_suggested) AS suggested,
              count(DISTINCT garment_set_hash)
                FILTER (WHERE was_suggested AND kind = 'worn') AS worn
            FROM outfit_feedback
            WHERE created_at > now() - make_interval(days => :days)
            """
        ),
        {"days": days},
    )
    r = row.mappings().one()
    suggested, worn = int(r["suggested"] or 0), int(r["worn"] or 0)
    return {
        "suggested_outfits": suggested,
        "worn_outfits": worn,
        # None, not 0.0, when nothing was suggested. A rate over no suggestions
        # is not 0% — it is unmeasured, and the difference decides whether the
        # ranking is bad or simply untested.
        "wear_through_rate": round(worn / suggested, 4) if suggested else None,
        "window_days": days,
    }


# ------------------------------------------------------ preference facts


@router.get("/me/preferences")
async def list_facts(user: CurrentUser, db: TenantDB) -> dict[str, Any]:
    rows = await db.execute(
        text(
            "SELECT id, kind::text AS kind, field_name, field_value, source, created_at "
            "FROM preference_fact ORDER BY created_at DESC"
        )
    )
    return {"facts": [dict(r) for r in rows.mappings()]}


@router.post("/me/preferences", status_code=status.HTTP_201_CREATED)
async def add_fact(body: FactRequest, user: CurrentUser) -> dict[str, Any]:
    """Assert a preference. Always `source='user'`.

    Nothing here can create an inferred fact: inference belongs to a background
    job, and letting a request write `source='inferred'` would make the two
    indistinguishable at exactly the point where the distinction matters —
    telling a user "you never wear yellow" in their own words when they never
    said it is how you lose their trust in one screen.
    """
    if body.field_name not in FACT_FIELDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"field_name must be one of {sorted(FACT_FIELDS)}",
        )

    async with tenant_session(user.id) as db:
        fact_id = uuid.uuid4()
        row = await db.execute(
            text(
                """
                INSERT INTO preference_fact
                    (id, user_id, kind, field_name, field_value, source)
                VALUES (:id, :uid, CAST(:kind AS preference_fact_kind),
                        :field, :value, 'user')
                ON CONFLICT (user_id, kind, field_name, field_value) DO UPDATE
                    SET source = 'user'
                RETURNING id
                """
            ),
            {
                "id": fact_id,
                "uid": user.id,
                "kind": body.kind,
                "field": body.field_name,
                "value": body.field_value,
            },
        )
        return {
            "id": str(row.scalar_one()),
            "kind": body.kind,
            "field_name": body.field_name,
            "field_value": body.field_value,
            "source": "user",
        }


@router.delete("/me/preferences/{fact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_fact(fact_id: uuid.UUID, user: CurrentUser) -> None:
    """Facts ARE deletable, unlike feedback events.

    A preference fact is a current belief the user is entitled to withdraw; a
    feedback event is something that happened and cannot un-happen. Conflating
    them would either make taste uncorrectable or make history rewritable.
    """
    async with tenant_session(user.id) as db:
        # RETURNING rather than `.rowcount`: SQLAlchemy's async `Result` does
        # not expose it, and a DELETE that matched nothing must be a 404 rather
        # than a silent 204 telling the user their fact is gone when it is not.
        result = await db.execute(
            text("DELETE FROM preference_fact WHERE id = :id RETURNING id"), {"id": fact_id}
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="fact not found")
