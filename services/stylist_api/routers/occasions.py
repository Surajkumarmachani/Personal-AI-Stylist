"""Custom occasions: a user's own name for an existing taxonomy occasion.

WHAT THIS IS NOT
----------------
It is not a nineteenth occasion. `taxonomy.yaml` is frozen at v1.0.0 and its
enums generate Postgres types, so a genuinely new occasion means ALTER TYPE on
a type every garment column depends on. See migration 0018.

An alias carries a `base_occasion` that IS a real taxonomy id, so
`resolve_context` always receives something it understands and the scorer,
precompute, bandit and reranker never learn that aliases exist. A feature that
requires no change to the ranking path cannot break the ranking path.

WHY OVERRIDES ARE ALLOWED AT ALL
--------------------------------
Without them this is just a nickname, and the thing people actually mean by
"my own occasion" is usually a calibration: "my office is more formal than you
think", "the farmhouse haldi is dressier than a normal mehendi". Formality and
dress code are exactly those two knobs, they are what `resolve_context` reads,
and both are validated against the taxonomy here — an out-of-range formality
would reach the scorer as a target no garment can match, which presents to the
user as "no outfits" with no reason given.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from stylist_api.deps import CurrentUser, TenantDB
from stylist_domain.taxonomy import load_taxonomy

router = APIRouter(tags=["occasions"])

# Enough to name a thing; short enough that it fits a tile and a chat message.
MAX_NAME = 60


class CustomOccasionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME)
    base_occasion: str
    formality_override: int | None = Field(default=None, ge=1, le=5)
    dress_code_override: str | None = None


def _validate(body: CustomOccasionRequest) -> None:
    """Check against the taxonomy, not against a copy of it.

    The base occasion and dress code are plain varchars in the database
    precisely so this table does not depend on the generated enum types — which
    moves the check here. Reading `load_taxonomy()` rather than a literal list
    means a renamed occasion fails loudly instead of silently accepting an id
    the scorer will later reject.
    """
    taxonomy = load_taxonomy()
    occasions = {o["id"] for o in taxonomy.raw["occasions"]}
    if body.base_occasion not in occasions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"base_occasion must be one of {sorted(occasions)}",
        )
    if body.dress_code_override is not None:
        codes = {str(c["id"]) for c in taxonomy.raw.get("dress_codes", [])}
        if codes and body.dress_code_override not in codes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"dress_code_override must be one of {sorted(codes)}",
            )


def _row(r: Any) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "name": r["name"],
        "base_occasion": r["base_occasion"],
        "formality_override": r["formality_override"],
        "dress_code_override": r["dress_code_override"],
    }


@router.get("/me/occasions")
async def list_custom(user: CurrentUser, db: TenantDB) -> dict[str, Any]:
    rows = await db.execute(
        text(
            "SELECT id, name, base_occasion, formality_override, dress_code_override "
            "FROM custom_occasion ORDER BY created_at"
        )
    )
    return {"items": [_row(r) for r in rows.mappings()]}


@router.post("/me/occasions", status_code=status.HTTP_201_CREATED)
async def create_custom(
    body: CustomOccasionRequest, user: CurrentUser, db: TenantDB
) -> dict[str, Any]:
    _validate(body)
    new_id = uuid.uuid4()
    try:
        await db.execute(
            text(
                """
                INSERT INTO custom_occasion (
                    id, user_id, name, base_occasion,
                    formality_override, dress_code_override
                ) VALUES (
                    :id, CAST(:uid AS uuid), :name, :base, :formality, :dress
                )
                """
            ),
            {
                "id": new_id,
                "uid": str(user.id),
                "name": body.name.strip(),
                "base": body.base_occasion,
                "formality": body.formality_override,
                "dress": body.dress_code_override,
            },
        )
    except Exception as exc:
        # The unique index is on (user_id, lower(name)) — two aliases differing
        # only in case are the same alias to the person who typed them, and the
        # intent lexicon lower-cases before matching, so the second would never
        # win a lookup. 409 rather than 500: this is a user-fixable collision.
        if "uq_custom_occasion_name" in str(exc):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"you already have an occasion called {body.name.strip()!r}",
            ) from exc
        raise
    return {
        "id": str(new_id),
        "name": body.name.strip(),
        "base_occasion": body.base_occasion,
        "formality_override": body.formality_override,
        "dress_code_override": body.dress_code_override,
    }


@router.delete("/me/occasions/{occasion_id}")
async def delete_custom(
    occasion_id: uuid.UUID, user: CurrentUser, db: TenantDB
) -> dict[str, Any]:
    # No user_id predicate: RLS scopes it, so another tenant's id is a 404
    # rather than a 403 and we do not confirm that the id exists.
    result = await db.execute(
        text("DELETE FROM custom_occasion WHERE id = :id RETURNING id"), {"id": occasion_id}
    )
    if result.first() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return {"id": str(occasion_id), "deleted": True}
