"""Outfit boards: render once, cache in object storage, serve a URL (Phase 8).

WHY THE RESPONSE IS A URL AND NOT THE IMAGE
-------------------------------------------
The exit criterion is "boards render < 200ms p95 **from CDN**". Streaming PNG
bytes through the API would put every board on the application's egress path
and make the number a property of our uvicorn workers instead of the CDN — and
it would be uncacheable by anything in front of us. The handler returns a
presigned URL to an object the CDN can hold, which is what makes the criterion
measurable in the first place.

RENDER ONCE, KEYED BY THE OUTFIT'S IDENTITY
--------------------------------------------
`boards/{user}/{garment_set_hash}.png`. The hash is over the SORTED garment
ids, so the same outfit in any order is one object — and because
`compose_board` is deterministic, a cache hit is provably the same image the
renderer would produce now. That is the property that makes serving a stale-
looking object safe: it is not stale, it is identical.

A garment changing (a re-matted cutout, a correction) does NOT invalidate the
board, and that is a known gap rather than an oversight — invalidation belongs
on the outbox event that already fires for those changes, and wiring it is a
step of its own. Boards are written with the cutout the user has TODAY; the
worst case is a board showing last week's cutout of a garment they still own.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import text

from stylist_api.deps import CurrentUser, ObjectStoreDep, TenantDB
from stylist_domain.board import MissingCutoutError, compose_board

logger = logging.getLogger(__name__)

router = APIRouter(tags=["boards"])

# Long, because a board is immutable for its key: the hash names the exact
# garment set and the renderer is deterministic, so there is nothing for a
# short TTL to protect against.
BOARD_URL_TTL_SECONDS = 24 * 3600


def board_key(user_id: uuid.UUID, garment_set_hash: str) -> str:
    return f"boards/{user_id}/{garment_set_hash}.png"


@router.get("/outfits/{garment_set_hash}/board")
async def get_board(
    garment_set_hash: str,
    user: CurrentUser,
    db: TenantDB,
    store: ObjectStoreDep,
    force: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """The board for one precomputed outfit. Rendered on first request."""
    if len(garment_set_hash) != 64 or not all(c in "0123456789abcdef" for c in garment_set_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="not a garment set hash"
        )

    key = board_key(user.id, garment_set_hash)
    if not force and store.head(key) is not None:
        return {
            "board_url": store.presign_download(key, ttl_seconds=BOARD_URL_TTL_SECONDS),
            "garment_set_hash": garment_set_hash,
            "rendered": False,
        }

    # RLS scopes this, so another tenant's outfit is simply not found. We do
    # not confirm the hash exists for somebody.
    row = await db.execute(
        text("SELECT garment_ids FROM outfits WHERE garment_set_hash = :h LIMIT 1"),
        {"h": garment_set_hash},
    )
    outfit = row.mappings().one_or_none()
    if outfit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="outfit not found")

    ids = [str(g) for g in outfit["garment_ids"]]
    garments = await db.execute(
        text(
            "SELECT id::text AS id, slot::text AS slot, cutout_key FROM garments "
            "WHERE id = ANY(CAST(:ids AS uuid[])) AND is_active"
        ),
        {"ids": ids},
    )
    rows = {r["id"]: dict(r) for r in garments.mappings()}

    missing = [g for g in ids if g not in rows or not rows[g]["cutout_key"]]
    if missing:
        # 409, not 500: nothing is broken, the outfit simply is not renderable
        # because a garment has no cutout yet. Distinguishing the two matters —
        # a 500 sends whoever sees it looking for a bug in the compositor, when
        # the actual answer is "that garment has not been matted".
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{len(missing)} garment(s) have no cutout; board cannot be rendered",
        )

    items: list[tuple[str, str, bytes]] = []
    for gid in ids:
        r = rows[gid]
        try:
            items.append((gid, r["slot"], store.get_bytes(r["cutout_key"])))
        except Exception as exc:
            logger.warning("board %s: cutout %s unreadable: %s", garment_set_hash, gid, exc)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"cutout for garment {gid} is not in storage",
            ) from exc

    try:
        png = compose_board(items)
    except MissingCutoutError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    store.put_bytes(key, png, content_type="image/png")
    return {
        "board_url": store.presign_download(key, ttl_seconds=BOARD_URL_TTL_SECONDS),
        "garment_set_hash": garment_set_hash,
        "rendered": True,
        "bytes": len(png),
    }
