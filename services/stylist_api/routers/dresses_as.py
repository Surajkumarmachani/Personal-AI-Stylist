"""Whose clothes to suggest buying (see migration 0026).

Asked at sign-up, changeable in Profile, and asked once on the home screen for
accounts made before the question existed. Two readers, both past the edge of
the wardrobe: the shop gap-filler and the shortfall advice. The user's own
clothes are never filtered by it — whatever they own, they own.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from stylist_api.deps import CurrentUser, TenantDB

router = APIRouter(tags=["profile"])


class DressesAsRequest(BaseModel):
    dresses_as: Literal["women", "men", "all"]


async def read_dresses_as(db: Any) -> str | None:
    """The stored answer, or None if never asked. Callers treat None as 'all'."""
    row = await db.execute(text("SELECT dresses_as FROM user_profile LIMIT 1"))
    value = row.scalar_one_or_none()
    return str(value) if value else None


@router.get("/me/dresses-as")
async def get_dresses_as(user: CurrentUser, db: TenantDB) -> dict[str, Any]:
    value = await read_dresses_as(db)
    # `asked` stated rather than inferred from a null, so the client does not
    # have to know that null means "show the question".
    return {"dresses_as": value, "asked": value is not None}


@router.put("/me/dresses-as")
async def set_dresses_as(
    body: DressesAsRequest, user: CurrentUser, db: TenantDB
) -> dict[str, Any]:
    await db.execute(
        text("UPDATE user_profile SET dresses_as = :v, updated_at = now()"),
        {"v": body.dresses_as},
    )
    return {"dresses_as": body.dresses_as, "asked": True}
