"""Garment field corrections (Step 4.3).

THE TWO MOST VALUABLE TELEMETRY EVENTS IN THE PRODUCT
-----------------------------------------------------
A correction is simultaneously:

  1. the live accuracy metric. §D1: "Correction rate is your live accuracy
     metric — it is labelled data arriving free." The golden set measures
     accuracy on 500 images once; corrections measure it on every real
     wardrobe, continuously, on the distribution that actually matters.
  2. training data. Phase 3.3's pattern classifier head needs ~2k labelled
     examples, and this is where they come from.

Both uses require the BEFORE value and the model's confidence at the time —
which is why corrections are an append-only log rather than an UPDATE. An
update would leave the right answer and destroy the evidence that we got it
wrong, along with any way to distinguish "confidently wrong" (a model problem)
from "correctly uncertain" (working as designed).

AND A CORRECTION IS PERMANENT
-----------------------------
Every corrected field is added to `garments.user_verified_fields`, and the tag
stage checks that column in SQL before writing. A backfill, a model upgrade, a
re-extraction — none of them can overwrite it. Without that guarantee the
correction UI is a lie: the user fixes a value, a backfill runs, and their fix
silently disappears.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from stylist_api.deps import CurrentUser, TenantDB
from stylist_db.outbox import emit
from stylist_db.session import tenant_session
from stylist_domain.taxonomy import load_taxonomy

router = APIRouter(tags=["corrections"])

# Fields a user may correct, mapped to their Postgres enum type (None = not an
# enum). Deliberately a closed list: accepting an arbitrary column name here
# would be an update-any-column primitive exposed to the internet.
CORRECTABLE: dict[str, str | None] = {
    "slot": "slot",
    "subcategory": "subcategory",
    "primary_colour": "colour",
    "secondary_colour": "colour",
    "pattern": "pattern",
    "material": "material",
    "fit": "fit",
    "dress_code": "dress_code",
    "formality": None,
    "warmth": None,
    # FREE TEXT, and the only two fields here that are. Every other entry is
    # held to a taxonomy enum because the VLM produces it and a value outside
    # the vocabulary is a bug. These are typed by the user, about their own
    # clothes, and there is no closed list of brands or a single size system —
    # see migration 0021.
    "brand": None,
    "size_label": None,
}

# Length caps matching the columns. Not validation of CONTENT — there is
# nothing to validate a brand against — but a bound, so a paste of a whole
# webpage is a 400 rather than a database error.
FREE_TEXT_LIMITS: dict[str, int] = {"brand": 80, "size_label": 40}


class CorrectionRequest(BaseModel):
    field_name: str = Field(examples=sorted(CORRECTABLE))
    # None is a legitimate correction: "this garment has no secondary colour"
    # is information, and the model having invented one is exactly the kind of
    # error worth recording.
    new_value: str | int | None = None

    @field_validator("field_name")
    @classmethod
    def _known_field(cls, v: str) -> str:
        if v not in CORRECTABLE:
            raise ValueError(f"{v} is not correctable; allowed: {sorted(CORRECTABLE)}")
        return v


class CorrectionResponse(BaseModel):
    garment_id: uuid.UUID
    field_name: str
    old_value: str | None
    new_value: str | None
    user_verified_fields: list[str]


def _validate_value(field_name: str, value: Any) -> Any:
    """Reject values the taxonomy does not contain.

    The same enums the VLM is held to. A user typing a value the database will
    refuse should get a clear 400, not a 500 from a failed cast — and a
    correction that cannot be stored is worse than no correction, because the
    user believes they fixed it.
    """
    if value is None:
        return None
    taxonomy = load_taxonomy()
    allowed: dict[str, tuple[str, ...]] = {
        "slot": taxonomy.slots,
        "subcategory": taxonomy.subcategories,
        "primary_colour": taxonomy.colours,
        "secondary_colour": taxonomy.colours,
        "pattern": taxonomy.patterns,
        "material": taxonomy.materials,
        "fit": taxonomy.fits,
        "dress_code": taxonomy.dress_codes,
    }
    if field_name in FREE_TEXT_LIMITS:
        text_value = str(value).strip()
        if not text_value:
            # Clearing is a legitimate correction: "I was wrong, I don't know
            # the brand". Stored as NULL rather than an empty string so the UI
            # has one falsy state to render, not two.
            return None
        if len(text_value) > FREE_TEXT_LIMITS[field_name]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{field_name} must be at most {FREE_TEXT_LIMITS[field_name]} characters",
            )
        return text_value

    if field_name in allowed:
        if value not in allowed[field_name]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"{value!r} is not a valid {field_name}. If a real garment "
                    f"cannot be described with the available values, that is a "
                    f"taxonomy gap worth reporting, not a value to invent."
                ),
            )
        return value
    # formality / warmth
    if not isinstance(value, int) or not (1 <= value <= 5):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be an integer 1-5",
        )
    return value


@router.patch(
    "/garments/{garment_id}/fields",
    response_model=CorrectionResponse,
)
async def correct_field(
    garment_id: uuid.UUID,
    body: CorrectionRequest,
    user: CurrentUser,
) -> CorrectionResponse:
    """Correct one field. Append-only log + permanent verification marker.

    Owns its transaction rather than taking `TenantDB`: FastAPI commits a
    `yield` dependency AFTER the response, so the correction UI — which
    re-reads the garment as soon as it gets 200 — could render the value the
    user just replaced. Latent here since Phase 4; found while fixing the same
    bug in the Phase 5 wear endpoints.
    """
    value = _validate_value(body.field_name, body.new_value)
    enum_type = CORRECTABLE[body.field_name]

    async with tenant_session(user.id) as db:
        return await _apply_correction(db, garment_id, body, user, value, enum_type)


async def _apply_correction(
    db: Any,
    garment_id: uuid.UUID,
    body: CorrectionRequest,
    user: Any,
    value: Any,
    enum_type: str | None,
) -> CorrectionResponse:

    row = await db.execute(
        text(
            f"SELECT {body.field_name}::text AS current, field_confidence, "
            "extractor_version FROM garments WHERE id = :gid AND is_active"
        ),
        {"gid": garment_id},
    )
    existing = row.mappings().one_or_none()
    if existing is None:
        # RLS scopes this, so another tenant's garment is a 404 — we do not
        # confirm the id exists.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="garment not found")

    old_value = existing["current"]
    confidence = (existing["field_confidence"] or {}).get(body.field_name)

    cast = f"CAST(:value AS {enum_type})" if enum_type else ":value"
    await db.execute(
        text(
            f"""
            UPDATE garments
            SET {body.field_name} = {cast},
                user_verified_fields =
                    CASE WHEN :field = ANY(user_verified_fields)
                         THEN user_verified_fields
                         ELSE array_append(user_verified_fields, :field) END,
                needs_review = false,
                updated_at = now()
            WHERE id = :gid
            """
        ),
        {"value": value, "field": body.field_name, "gid": garment_id},
    )

    await db.execute(
        text(
            """
            INSERT INTO garment_corrections
                (id, user_id, garment_id, field_name, old_value, new_value,
                 extractor_version, model_confidence)
            VALUES (:id, :uid, :gid, :field, :old, :new, :version, :confidence)
            """
        ),
        {
            "id": uuid.uuid4(),
            "uid": user.id,
            "gid": garment_id,
            "field": body.field_name,
            "old": None if old_value is None else str(old_value),
            "new": None if value is None else str(value),
            "version": existing["extractor_version"],
            "confidence": confidence,
        },
    )

    # Same transaction as the correction (§C1). Phase 6 consumes this to
    # invalidate precomputed outfits — a garment the user just re-tagged should
    # not keep appearing in suggestions built on the wrong tags.
    await emit(
        db,
        aggregate_id=garment_id,
        user_id=user.id,
        event_type="garment.field_corrected",
        payload={
            "garment_id": str(garment_id),
            "field": body.field_name,
            "from": None if old_value is None else str(old_value),
            "to": None if value is None else str(value),
            "model_confidence": float(confidence) if confidence is not None else None,
        },
    )

    verified = await db.execute(
        text("SELECT user_verified_fields FROM garments WHERE id = :gid"),
        {"gid": garment_id},
    )
    return CorrectionResponse(
        garment_id=garment_id,
        field_name=body.field_name,
        old_value=None if old_value is None else str(old_value),
        new_value=None if value is None else str(value),
        user_verified_fields=list(verified.scalar_one() or []),
    )


@router.get("/garments/{garment_id}/detail")
async def garment_detail(garment_id: uuid.UUID, user: CurrentUser, db: TenantDB) -> dict[str, Any]:
    """Everything the correction UI needs for one garment.

    Includes per-field confidence and which fields the user has already
    verified, because the UI has to show WHY a field is flagged. A review badge
    with no explanation just tells the user something is wrong without telling
    them what to look at.
    """
    row = await db.execute(
        text(
            """
            SELECT id, slot, subcategory, primary_colour, secondary_colour,
                   pattern, material, fit, dress_code, formality, warmth,
                   brand, size_label,
                   state, needs_review, cutout_key, field_confidence,
                   user_verified_fields, extractor_version, moderation,
                   -- Phase 5: the correction UI shows laundry state and any
                   -- outstanding duplicate question alongside the tags.
                   needs_wash, duplicate_of, purchase_price_minor,
                   purchase_currency, embedding_version,
                   attributes_raw->'tag'->>'degraded' AS tag_degraded,
                   attributes_raw->'tag'->>'reason'   AS tag_reason
            FROM garments WHERE id = :gid AND is_active
            """
        ),
        {"gid": garment_id},
    )
    garment = row.mappings().one_or_none()
    if garment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="garment not found")

    taxonomy = load_taxonomy()
    return {
        "garment": {k: v for k, v in dict(garment).items() if k != "cutout_key"},
        # The UI renders dropdowns from these, so it can never offer a value
        # the database would reject.
        "options": {
            "slot": list(taxonomy.slots),
            "subcategory": list(taxonomy.subcategories),
            "primary_colour": list(taxonomy.colours),
            "secondary_colour": list(taxonomy.colours),
            "pattern": list(taxonomy.patterns),
            "material": list(taxonomy.materials),
            "fit": list(taxonomy.fits),
            "dress_code": list(taxonomy.dress_codes),
            "formality": [1, 2, 3, 4, 5],
            "warmth": [1, 2, 3, 4, 5],
        },
        "review_below": {name: cfg.get("review_below") for name, cfg in taxonomy.fields.items()},
    }


@router.get("/ops/correction-rate")
async def correction_rate(
    user: CurrentUser,
    db: TenantDB,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> dict[str, Any]:
    """Per-field correction rate — the live accuracy metric (§D1, §D3 dashboard 4).

    Scoped to the calling tenant by RLS. A cross-tenant version belongs on the
    admin surface with its own authorisation, not here: an endpoint that
    aggregates every user's data is not something a user token should reach.
    """
    rows = await db.execute(
        text(
            """
            SELECT c.field_name,
                   count(*) AS corrections,
                   avg(c.model_confidence) AS avg_model_confidence
            FROM garment_corrections c
            WHERE c.created_at > now() - make_interval(days => :days)
            GROUP BY c.field_name
            ORDER BY corrections DESC
            """
        ),
        {"days": days},
    )
    corrections = [dict(r) for r in rows.mappings()]

    total = await db.execute(
        text("SELECT count(*) FROM garments WHERE is_active AND state <> 'received'")
    )
    garments = int(total.scalar_one())

    return {
        "window_days": days,
        "garments": garments,
        "by_field": [
            {
                "field": item["field_name"],
                "corrections": item["corrections"],
                # The rate that matters: how often this field is wrong per
                # garment. Phase 5's go/no-go is "above ~20% on any field, fix
                # ingestion before building recommendations".
                "rate": round(item["corrections"] / garments, 4) if garments else None,
                "avg_model_confidence": (
                    float(item["avg_model_confidence"])
                    if item["avg_model_confidence"] is not None
                    else None
                ),
            }
            for item in corrections
        ],
    }
