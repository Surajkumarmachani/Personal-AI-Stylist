"""Wear log, laundry state and cost-per-wear (Step 5.2).

WHY A LOG AND NOT A COUNTER
---------------------------
Every question worth asking here is about WHEN, not how many:

  - "your 20 most-worn" — the onboarding flow, and the ranking that makes the
    first session useful instead of an empty grid
  - "not worn in 90 days" — the prompt that surfaces a wardrobe's dead weight
  - cost-per-wear — price divided by a count that must be attributable to
    dates, or it cannot be recomputed when a mis-tap is deleted

A counter answers the third badly and the first two not at all, and it cannot
be corrected: decrementing an integer whose history is gone leaves you unable
to say whether the new value is right.

WHY THE WRITE HANDLERS OPEN THEIR OWN TRANSACTION
-------------------------------------------------
FastAPI runs the teardown of a `yield` dependency AFTER the response is sent,
so a handler taking `TenantDB` has its COMMIT happen after the client already
has its 200. A client that does the obvious thing — delete a wearing, then
immediately re-read the count — can observe the pre-delete value.

It is a race, so it hides. This surfaced as a 1-in-4 failure of a single check
in the Phase 5 e2e script ("a mis-tap can be undone"), which is exactly how
this class of bug presents: rare, unreproducible, and indistinguishable from
flakiness until you look at dependency ordering rather than at the query.
`garments.py` documents the same reasoning for ingest.

ONE WEARING PER GARMENT PER DAY
-------------------------------
Enforced by a unique index, not by the handler. Tapping "worn today" twice
records the same fact twice, and cost-per-wear silently halves. The second tap
is idempotent and returns the existing row.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from stylist_api.deps import CurrentUser, ObjectStoreDep, TenantDB
from stylist_db.outbox import emit
from stylist_db.session import tenant_session

router = APIRouter(tags=["wear"])


class WearRequest(BaseModel):
    # Defaults to today, but explicit dates are allowed: people log yesterday's
    # outfit the next morning, and refusing that quietly loses the data.
    worn_on: date | None = None
    note: str | None = Field(default=None, max_length=280)


class WearResponse(BaseModel):
    garment_id: uuid.UUID
    worn_on: date
    total_wears: int
    already_logged: bool
    cost_per_wear_minor: int | None
    currency: str | None


class LaundryRequest(BaseModel):
    needs_wash: bool


async def _require_garment(db: TenantDB, garment_id: uuid.UUID) -> dict[str, Any]:
    row = await db.execute(
        text(
            "SELECT id, purchase_price_minor, purchase_currency "
            "FROM garments WHERE id = :gid AND is_active"
        ),
        {"gid": garment_id},
    )
    garment = row.mappings().one_or_none()
    if garment is None:
        # RLS scopes this, so another tenant's garment is a 404. We do not
        # confirm the id exists.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="garment not found")
    return dict(garment)


@router.post("/garments/{garment_id}/wear", response_model=WearResponse)
async def log_wear(garment_id: uuid.UUID, body: WearRequest, user: CurrentUser) -> WearResponse:
    """Record that this garment was worn. Idempotent per day."""
    worn = body.worn_on or date.today()
    if worn > date.today():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot log a wearing in the future",
        )

    async with tenant_session(user.id) as db:
        garment = await _require_garment(db, garment_id)
        return await _record_wear(db, user, garment, garment_id, worn, body.note)


async def _record_wear(
    db: Any,
    user: Any,
    garment: dict[str, Any],
    garment_id: uuid.UUID,
    worn: date,
    note: str | None,
) -> WearResponse:
    # ON CONFLICT DO NOTHING + RETURNING tells us whether this was the first
    # tap without a separate SELECT that another request could race.
    inserted = await db.execute(
        text(
            """
            INSERT INTO wear_log (id, user_id, garment_id, worn_on, note)
            VALUES (:id, :uid, :gid, :worn, :note)
            ON CONFLICT (garment_id, worn_on) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": uuid.uuid4(),
            "uid": user.id,
            "gid": garment_id,
            "worn": worn,
            "note": note,
        },
    )
    already = inserted.scalar_one_or_none() is None

    # Wearing it takes it out of the clean pile. Not the other way round: a
    # garment can be clean and unworn, but it cannot be worn and still crisp.
    await db.execute(
        text("UPDATE garments SET needs_wash = true, updated_at = now() WHERE id = :gid"),
        {"gid": garment_id},
    )

    total = await db.execute(
        text("SELECT count(*) FROM wear_log WHERE garment_id = :gid"), {"gid": garment_id}
    )
    wears = int(total.scalar_one())

    if not already:
        # Phase 6 consumes this: a garment worn today should not be top of
        # tomorrow's suggestions.
        await emit(
            db,
            aggregate_id=garment_id,
            user_id=user.id,
            event_type="garment.worn",
            payload={"garment_id": str(garment_id), "worn_on": worn.isoformat()},
        )

    price = garment["purchase_price_minor"]
    return WearResponse(
        garment_id=garment_id,
        worn_on=worn,
        total_wears=wears,
        already_logged=already,
        # Integer division in minor units: the answer is money, and a float
        # here would print ₹33.333333 in a UI that has no business rounding it.
        cost_per_wear_minor=(int(price) // wears) if price and wears else None,
        currency=garment["purchase_currency"],
    )


@router.delete("/garments/{garment_id}/wear/{worn_on}", status_code=status.HTTP_204_NO_CONTENT)
async def unlog_wear(garment_id: uuid.UUID, worn_on: date, user: CurrentUser, db: TenantDB) -> None:
    """Undo a mis-tap. The reason the log is rows and not a counter."""
    await _require_garment(db, garment_id)
    await db.execute(
        text("DELETE FROM wear_log WHERE garment_id = :gid AND worn_on = :worn"),
        {"gid": garment_id, "worn": worn_on},
    )


@router.patch("/garments/{garment_id}/laundry")
async def set_laundry(
    garment_id: uuid.UUID, body: LaundryRequest, user: CurrentUser
) -> dict[str, Any]:
    """Move a garment in or out of the wash basket."""
    async with tenant_session(user.id) as db:
        await _require_garment(db, garment_id)
        await db.execute(
            text("UPDATE garments SET needs_wash = :w, updated_at = now() WHERE id = :gid"),
            {"w": body.needs_wash, "gid": garment_id},
        )
    return {"garment_id": str(garment_id), "needs_wash": body.needs_wash}


@router.get("/garments/{garment_id}/wears")
async def wear_history(
    garment_id: uuid.UUID,
    user: CurrentUser,
    db: TenantDB,
    limit: Annotated[int, Query(ge=1, le=365)] = 90,
) -> dict[str, Any]:
    garment = await _require_garment(db, garment_id)
    rows = await db.execute(
        text(
            "SELECT worn_on, note FROM wear_log WHERE garment_id = :gid "
            "ORDER BY worn_on DESC LIMIT :lim"
        ),
        {"gid": garment_id, "lim": limit},
    )
    wears = [{"worn_on": r["worn_on"].isoformat(), "note": r["note"]} for r in rows.mappings()]
    total = await db.execute(
        text("SELECT count(*) FROM wear_log WHERE garment_id = :gid"), {"gid": garment_id}
    )
    count = int(total.scalar_one())
    price = garment["purchase_price_minor"]
    return {
        "garment_id": str(garment_id),
        "total_wears": count,
        "cost_per_wear_minor": (int(price) // count) if price and count else None,
        "currency": garment["purchase_currency"],
        "wears": wears,
    }


@router.get("/wardrobe/worn-on")
async def worn_on(
    user: CurrentUser,
    db: TenantDB,
    store: ObjectStoreDep,
    on: Annotated[date | None, Query()] = None,
) -> dict[str, Any]:
    """What this user actually wore on a given day. Defaults to today.

    The home screen asks this BEFORE it offers anything. Someone who has
    already dressed does not need to be sold an outfit; showing suggestions
    over the top of a decision they have made reads as the product not
    listening. So this answers "are we done here?" and the suggestions only
    appear when the answer is no.

    `worn_on` is a DATE, not a timestamp, and the default is the server's
    today. A user in another timezone logging an outfit late in the evening
    can therefore see it attributed to the following day -- honest to record
    and wrong to the user. Fixing it properly needs the user's timezone, which
    this system does not collect; noting it here rather than pretending the
    date is unambiguous.
    """
    rows = await db.execute(
        text(
            """
            SELECT g.id, g.slot::text AS slot, g.subcategory::text AS subcategory,
                   g.primary_colour::text AS primary_colour, g.cutout_key,
                   w.worn_on, w.note
            FROM wear_log w
            JOIN garments g ON g.id = w.garment_id
            WHERE w.worn_on = COALESCE(:on, CURRENT_DATE) AND g.is_active
            ORDER BY g.slot::text, g.subcategory::text
            """
        ),
        {"on": on},
    )
    items = [
        {
            "id": str(r["id"]),
            "slot": r["slot"],
            "subcategory": r["subcategory"],
            "primary_colour": r["primary_colour"],
            "cutout_url": store.presign_download(r["cutout_key"]) if r["cutout_key"] else None,
            "note": r["note"],
        }
        for r in rows.mappings()
    ]
    return {
        "date": (on or date.today()).isoformat(),
        "items": items,
        # Stated rather than left to `len(items)`, because the home screen
        # branches on it and "did they dress today" is the question, not "how
        # many garments came back".
        "wore_something": bool(items),
    }


@router.get("/wardrobe/most-worn")
async def most_worn(
    user: CurrentUser,
    db: TenantDB,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """The ranking the onboarding flow is built on (Step 5.5).

    20 by default because that is the number the plan's onboarding asks for:
    "start with your 20 most-worn". Every comparison review says this is what
    separates users who stick from users who abandon — a wardrobe of 20 things
    you actually wear is useful immediately, where a half-catalogued closet of
    200 is a chore with no payoff.
    """
    rows = await db.execute(
        text(
            """
            SELECT g.id, g.subcategory::text AS subcategory,
                   g.primary_colour::text AS primary_colour, g.cutout_key,
                   count(w.id) AS wears, max(w.worn_on) AS last_worn,
                   g.purchase_price_minor, g.purchase_currency
            FROM garments g
            LEFT JOIN wear_log w ON w.garment_id = g.id
            WHERE g.is_active AND g.state NOT IN ('rejected', 'quarantined')
            GROUP BY g.id
            ORDER BY count(w.id) DESC, max(w.worn_on) DESC NULLS LAST
            LIMIT :lim
            """
        ),
        {"lim": limit},
    )
    items = []
    for r in rows.mappings():
        wears = int(r["wears"])
        price = r["purchase_price_minor"]
        items.append(
            {
                "id": str(r["id"]),
                "subcategory": r["subcategory"],
                "primary_colour": r["primary_colour"],
                "wears": wears,
                "last_worn": r["last_worn"].isoformat() if r["last_worn"] else None,
                "cost_per_wear_minor": (int(price) // wears) if price and wears else None,
                "currency": r["purchase_currency"],
            }
        )
    return {"items": items, "count": len(items)}
