"""The profile picture.

WHY IT IS NOT JUST ANOTHER UPLOAD
---------------------------------
It lands under its own `avatars/` prefix, alongside but separate from
`originals/` (garments) and `body/` (try-on consent material). Phase 8 made
that argument for body photos — "a photograph of a person is never
indistinguishable from a photograph of a shirt to a prefix operation" — and an
avatar is a third category again: it is drawn in the app chrome on every
screen, where a body photo is consented material used once for a render.

Keeping them apart is what lets "change my picture", "revoke try-on consent"
and "erase my account" each target exactly the right objects. Deleting an
avatar must not touch try-on consent, and revoking consent must not blank the
user's face out of the top bar.

THE URL IS PRESIGNED AND SHORT-LIVED, NOT PUBLIC
------------------------------------------------
The bucket is private, so the client gets a signed URL rather than a path.
That means it EXPIRES — `GET /me/avatar` is therefore cheap and called on
each load rather than cached into a session, and the TTL is generous enough
that a page open for a while does not show a broken image.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text

from stylist_api.deps import CurrentUser, ObjectStoreDep, TenantDB

router = APIRouter(tags=["avatar"])

AVATAR_PREFIX = "avatars"

# Long enough that a tab left open still renders it, short enough that a
# leaked URL is not a lasting handle on someone's face.
AVATAR_URL_TTL_SECONDS = 3600


class AvatarCommit(BaseModel):
    key: str


@router.post("/me/avatar/presign")
async def presign_avatar(user: CurrentUser, store: ObjectStoreDep) -> dict[str, Any]:
    """A presigned upload under `avatars/`."""
    presigned = store.presign_upload(
        user_id=user.id, content_type="image/jpeg", prefix=AVATAR_PREFIX
    )
    return {
        "upload_id": presigned.upload_id,
        "key": presigned.key,
        "url": presigned.url,
        "fields": presigned.fields,
        # Always a presigned POST here — see stylist_clients.storage for
        # why this project does not use PUT urls.
        "method": "POST",
    }


@router.put("/me/avatar")
async def set_avatar(
    body: AvatarCommit, user: CurrentUser, db: TenantDB, store: ObjectStoreDep
) -> dict[str, Any]:
    """Point the profile at an uploaded object.

    THE KEY IS CHECKED, NOT TRUSTED. It arrives from the client, and a client
    that sent `originals/<someone else>/...` would otherwise have this endpoint
    happily store a pointer to another tenant's photograph. Two conditions,
    both required: the prefix must be `avatars/`, and the path must be inside
    THIS user's folder — which is how `presign_upload` lays keys out.
    """
    expected_prefix = f"{AVATAR_PREFIX}/{user.id}/"
    if not body.key.startswith(expected_prefix):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="key is not an avatar upload belonging to this account",
        )
    if store.head(body.key) is None:
        # Committing a key with nothing behind it would leave the profile
        # pointing at a 404 and the UI showing a broken image forever.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no object at that key; upload it before committing",
        )

    await db.execute(
        text("UPDATE user_profile SET avatar_key = :k, updated_at = now()"),
        {"k": body.key},
    )
    return {
        "avatar_url": store.presign_download(body.key, ttl_seconds=AVATAR_URL_TTL_SECONDS),
    }


@router.get("/me/avatar")
async def get_avatar(user: CurrentUser, db: TenantDB, store: ObjectStoreDep) -> dict[str, Any]:
    """A fresh signed URL, or null. Null is the normal state."""
    row = await db.execute(text("SELECT avatar_key FROM user_profile LIMIT 1"))
    key = row.scalar_one_or_none()
    return {
        "avatar_url": (
            store.presign_download(key, ttl_seconds=AVATAR_URL_TTL_SECONDS) if key else None
        )
    }


@router.delete("/me/avatar")
async def clear_avatar(user: CurrentUser, db: TenantDB) -> dict[str, Any]:
    """Forget the picture.

    Clears the POINTER and leaves the object. Deleting user pixels is the
    erasure saga's job (§C5), which is audited, staged and covers every
    version in a versioned bucket — doing a fraction of that here would make
    the strong guarantee harder to reason about, not easier.
    """
    await db.execute(
        text("UPDATE user_profile SET avatar_key = NULL, updated_at = now()")
    )
    return {"avatar_url": None, "cleared": True}
