"""Render one outfit onto the owner's body photo (Phase 10).

WHY THIS IS A JOB AND NOT A HANDLER
------------------------------------
A diffusion render is 30-120s on shared hardware, and the queue in front of it
is other people's traffic. Holding an HTTP request open for that would occupy a
uvicorn worker for two minutes per outfit and blow every latency SLO in §B1 —
so the request enqueues and returns the board, and the render lands in object
storage where the next request finds it. Same shape as boards: render once,
key by the outfit's identity, serve a URL.

WHY A CHAIN AND NOT ONE CALL
-----------------------------
Every one of these models puts ONE garment on ONE body. An outfit is a shirt
AND trousers, so the render is sequential: the output of each pass becomes the
person image of the next.

The order is LOWER then UPPER, which is not arbitrary. The last garment applied
is the one that layers on top, and an untucked shirt hangs over the waistband
rather than under it. Reversing it produces trousers drawn over the shirt hem —
a detail nobody articulates but everybody sees.

Capped at MAX_PASSES. Each pass compounds the previous pass's artifacts as well
as costing another 30-120s, and a three-garment render is both the slowest and
the least accurate thing this system can produce.

WHAT IT REFUSES TO RENDER
-------------------------
Garments whose slot has no honest category (`drape` — a saree), and garments
the configured provider does not support (IDM-VTON is upper-body only). These
are SKIPPED AND REPORTED, never coerced into the nearest category: §C3's
argument throughout is that a confident wrong answer costs more than an absent
one, and that applies with more force to a picture of someone's own body.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from typing import Any

from sqlalchemy import text

from stylist_clients.vton_client import SLOT_TO_CATEGORY, VTONClient, VTONUnavailable
from stylist_db.session import tenant_session
from stylist_obs import stage_span

logger = logging.getLogger(__name__)

# Two renders is already up to four minutes. A third compounds artifacts for a
# garment that is usually an outer layer the models render worst anyway.
MAX_PASSES = 2

# Lower first so the upper garment layers over it. See the module docstring.
PASS_ORDER = {"lower": 0, "full_body": 0, "upper_base": 1, "upper_layer": 2}


# Below this mean per-pixel difference, the "render" is the input photo handed
# back unchanged.
#
# THE BUG THIS CATCHES, found 2026-09-18 on the first render that ever
# returned True: a provider echoed the body photo verbatim, the pipeline got
# valid PNG bytes back, stored them, and reported `rendered: True`. The image
# was the person in their own clothes. Nothing in the response said otherwise
# and the UI would have shown it as a try-on.
#
# `rendered: True` meant "bytes came back", not "a garment was fitted" — the
# same class of failure as the cache_hit header, the RLS zero-rows read and the
# style vector nothing consumed: a signal that looks live and cannot answer its
# own question. Eighth instance in this codebase.
#
# 2.0/255 rather than exact equality: the provider re-encodes, so a genuine
# passthrough still differs by JPEG/PNG rounding. A real fit changes the torso
# entirely and scores far higher — the measured passthrough was 0.0.
PASSTHROUGH_MAX_DIFF = 2.0


def _looks_unchanged(before: bytes, after: bytes) -> bool:
    """True when `after` is `before` re-encoded rather than rendered.

    Compared on a downscaled greyscale copy: the question is "is this the same
    picture", which survives a thumbnail, and a full-resolution RGB diff would
    cost more than the render it is checking.
    """
    from PIL import Image

    try:
        a = Image.open(io.BytesIO(before)).convert("L").resize((64, 64))
        b = Image.open(io.BytesIO(after)).convert("L").resize((64, 64))
    except Exception:  # pragma: no cover - unreadable bytes fail elsewhere
        return False
    # `list(img.getdata())` rather than iterating the ImagingCore directly:
    # Pillow's stubs do not declare it iterable, and bytes() gives a flat,
    # typed sequence for a greyscale image.
    pixels_a = bytes(a.tobytes())
    pixels_b = bytes(b.tobytes())
    if len(pixels_a) != len(pixels_b):
        return False
    total = sum(abs(x - y) for x, y in zip(pixels_a, pixels_b, strict=True))
    return bool((total / len(pixels_a)) < PASSTHROUGH_MAX_DIFF)



async def _record_failure(uid: uuid.UUID, garment_set_hash: str, reason: str) -> None:
    """Write the failure where the API can SEE it.

    The API documents a `render failed` state -- "the provider erred or
    returned nothing usable" -- and this function is what makes that state
    reachable. Until it existed, a failed render returned its reason into
    arq's Redis result, which the API never reads, so a render that died in
    1.7 seconds against a dead endpoint was indistinguishable from one still
    working: the card said "queued, tap again to check" forever, and every tap
    spent quota on a job that had already failed.

    `audit_log` rather than a new table: it already carries `user_id`,
    `subject_id` and a jsonb `detail`, it is already the try-on quota counter,
    and one row per failed render alongside the row for the request that
    caused it keeps the sequence readable by anyone debugging it.
    """
    async with tenant_session(uid) as session:
        await session.execute(
            text(
                "INSERT INTO audit_log (id, user_id, action, subject_type, subject_id, detail) "
                "VALUES (:id, :uid, 'tryon.failed', 'outfit', NULL, CAST(:detail AS jsonb))"
            ),
            {
                "id": uuid.uuid4(),
                "uid": uid,
                "detail": json.dumps({"garment_set_hash": garment_set_hash, "reason": reason}),
            },
        )



async def _record_render(
    uid: uuid.UUID, garment_set_hash: str, rendered: list[str], skipped: list[str]
) -> None:
    """Record which slots a successful render covered, and which it did not."""
    async with tenant_session(uid) as session:
        await session.execute(
            text(
                "INSERT INTO audit_log (id, user_id, action, subject_type, detail) "
                "VALUES (:id, :uid, 'tryon.rendered', 'outfit', CAST(:detail AS jsonb))"
            ),
            {
                "id": uuid.uuid4(),
                "uid": uid,
                "detail": json.dumps(
                    {
                        "garment_set_hash": garment_set_hash,
                        "rendered_slots": rendered,
                        "skipped_slots": sorted(set(skipped)),
                    }
                ),
            },
        )



async def _restore_head(original: bytes, rendered: bytes) -> bytes:
    """Put the user's OWN head back over a render. Best effort.

    WHY THIS IS NECESSARY
    ---------------------
    A try-on provider does not paste a garment onto a photo. It regenerates
    the entire frame, so the face comes back as a plausible stranger: measured
    on one real render, the subject's beard and sunglasses were gone, the
    features were somebody else's, and a cap appeared that was never there.
    For a product whose whole promise is "how would *I* look in this", that is
    the difference between useful and worthless.

    Only the garment region ever had to change. The head did not. So the head
    is taken from the ORIGINAL photo, using the ATR parse of that same photo
    as a stencil, and composited back.

    FEATHERED, not a hard cut. A 1-bit stencil edge against regenerated skin
    reads as a paste-up; a few pixels of blur across the boundary makes the
    seam disappear without moving it.

    BEST EFFORT, deliberately. If the parse fails, the sizes disagree, or the
    ML service is down, the render is returned UNCHANGED rather than failing:
    a render with the wrong face is still worth more than no render, and this
    is an enhancement to a picture that already exists.
    """
    try:
        from PIL import Image, ImageFilter

        from stylist_api.settings import get_settings
        from stylist_clients.ml_client import MLClient

        settings = get_settings()
        ml = MLClient(base_url=getattr(settings, "ml_base_url", ""))
        stencil_png = await ml.head_mask(image_bytes=original)

        base = Image.open(io.BytesIO(rendered)).convert("RGB")
        head_src = Image.open(io.BytesIO(original)).convert("RGB")
        stencil = Image.open(io.BytesIO(stencil_png)).convert("L")

        # The provider may return a different size than it was given. Line
        # everything up on the RENDER, which is what the user will see.
        if head_src.size != base.size:
            head_src = head_src.resize(base.size, Image.Resampling.LANCZOS)
        if stencil.size != base.size:
            stencil = stencil.resize(base.size, Image.Resampling.NEAREST)

        if stencil.getbbox() is None:
            # No head found in the photo. Nothing to restore, and pasting an
            # empty stencil would be a no-op with extra steps.
            return rendered

        base.paste(head_src, (0, 0), stencil.filter(ImageFilter.GaussianBlur(2.5)))
        out = io.BytesIO()
        base.save(out, format="PNG")
        return out.getvalue()
    except Exception as exc:
        logger.warning("tryon: head restore skipped: %s", exc)
        return rendered


async def render_tryon(
    ctx: dict[str, Any], *, user_id: str, aggregate_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Render one outfit. The outbox's signature, not one of our choosing.

    Never raises. Every failure path leaves the object absent, which the API
    reads as "not rendered" and answers with the board — the Phase 10 exit
    criterion is "works or degrades to a board, NEVER errors", and a job that
    raises would turn into a DLQ entry and an alert for what is a normal,
    expected outcome on a free shared GPU.
    """
    uid = uuid.UUID(user_id)
    garment_set_hash = str(payload.get("garment_set_hash") or aggregate_id)

    # Deferred, and imported FROM the API router, exactly as precompute.py
    # does for board_key: the HTTP surface owns the key because it is the
    # thing that has to find the object again.
    from stylist_api.routers.tryon import tryon_key
    from stylist_api.settings import get_settings
    from stylist_worker import deps

    settings = get_settings()
    store = ctx.get("object_store") or deps.get_object_store()

    provider = getattr(settings, "vton_provider", "")
    if not provider:
        return {"rendered": False, "reason": "no provider configured"}

    async with tenant_session(uid) as db:
        # Consent is re-read HERE, not trusted from the enqueueing request.
        # Between the tap and the render there is a queue, and a revocation
        # that lands in that window must stop the render — checking only at
        # enqueue time would send a body photo to a third party seconds after
        # the owner told us not to.
        consented = await db.execute(
            text(
                "SELECT object_key FROM body_photo WHERE revoked_at IS NULL "
                "ORDER BY consented_at DESC LIMIT 1"
            )
        )
        body_key = consented.scalar_one_or_none()
        if not body_key:
            return {"rendered": False, "reason": "consent revoked before render"}

        outfit = await db.execute(
            text("SELECT garment_ids FROM outfits WHERE garment_set_hash = :h LIMIT 1"),
            {"h": garment_set_hash},
        )
        row = outfit.mappings().one_or_none()
        if row is None:
            return {"rendered": False, "reason": "outfit not found"}

        garments = await db.execute(
            text(
                "SELECT id::text AS id, slot::text AS slot, cutout_key FROM garments "
                "WHERE id = ANY(CAST(:ids AS uuid[])) AND is_active"
            ),
            {"ids": [str(g) for g in row["garment_ids"]]},
        )
        items = [dict(r) for r in garments.mappings()]

    renderable = [g for g in items if g["slot"] in SLOT_TO_CATEGORY and g["cutout_key"]]
    skipped = [g["slot"] for g in items if g["slot"] not in SLOT_TO_CATEGORY]
    if not renderable:
        await _record_failure(uid, garment_set_hash, "no renderable garment")
        return {"rendered": False, "reason": "no renderable garment", "skipped": skipped}

    client = VTONClient(
        provider,
        api_token=getattr(settings, "vton_api_token", ""),
        base_url=getattr(settings, "vton_base_url", ""),
        timeout=getattr(settings, "vton_timeout_s", 900.0),
    )

    # DROP CATEGORIES THIS PROVIDER CANNOT RENDER, BEFORE ordering and capping.
    #
    # Several providers are upper-body only — IDM-VTON takes no category
    # argument at all, and the Colab wrapper removed it. Previously an outfit
    # whose FIRST pass was the trousers abandoned the whole render, because the
    # loop below treats a failure with `passes == 0` as "nothing worked". An
    # unsupported category is not a provider failure: the shirt in the same
    # outfit is perfectly renderable, and giving up on it produced "no render"
    # for outfits that were half-renderable all along.
    #
    # Filtering here rather than inside the loop also means MAX_PASSES applies
    # to garments that can actually be rendered, instead of being spent on ones
    # that were going to be refused.
    supported = client.profile.categories
    unsupported = [g["slot"] for g in renderable if SLOT_TO_CATEGORY[g["slot"]] not in supported]
    renderable = [g for g in renderable if SLOT_TO_CATEGORY[g["slot"]] in supported]
    if not renderable:
        return {
            "rendered": False,
            "reason": f"{provider} renders only "
            f"{sorted(client.profile.categories)}; nothing in this outfit qualifies",
            "skipped": skipped + unsupported,
        }

    renderable.sort(key=lambda g: PASS_ORDER.get(g["slot"], 99))
    # CONFIGURABLE, because it is the single biggest quality lever here and
    # the right value is a judgement, not a constant.
    #
    # Each pass feeds the PREVIOUS pass's output back in as the person image,
    # so the frame is regenerated once per garment. Two passes dress the
    # outfit more completely and degrade identity roughly twice as much:
    # measured on a real photo, two passes returned a different face, removed
    # the subject's beard and sunglasses, turned white trainers black and
    # added a cap. One pass dresses less and looks more like the person.
    max_passes = max(1, int(getattr(settings, "tryon_max_passes", MAX_PASSES)))
    dropped = [g["slot"] for g in renderable[max_passes:]]
    renderable = renderable[:max_passes]
    skipped = skipped + unsupported

    try:
        # Kept separately from `current`: `current` is overwritten by each
        # pass, and the head has to come from the photo the user uploaded.
        original = store.get_bytes(body_key)
        current = original
    except Exception as exc:
        logger.warning("tryon %s: body photo unreadable: %s", garment_set_hash, exc)
        await _record_failure(uid, garment_set_hash, "body photo unreadable")
        return {"rendered": False, "reason": "body photo unreadable"}

    passes = 0
    for garment in renderable:
        category = SLOT_TO_CATEGORY[garment["slot"]]
        try:
            with stage_span("vton_render"):
                produced = client.render(
                    person_png=current,
                    garment_png=store.get_bytes(garment["cutout_key"]),
                    category=category,
                )
            # THE PROVIDER MUST ACTUALLY HAVE CHANGED THE PICTURE. See
            # PASSTHROUGH_MAX_DIFF: a provider that echoes the input yields
            # valid bytes and a `rendered: True` that means nothing.
            if _looks_unchanged(current, produced):
                raise VTONUnavailable(
                    f"{provider} returned the body photo unchanged for slot "
                    f"{garment['slot']} — it accepted the request but fitted nothing"
                )
            current = produced
            passes += 1
        except VTONUnavailable as exc:
            # Logged at WARNING with the provider's own words. This is the only
            # place the ZeroGPU-without-a-token signature is visible, and
            # replacing it with a tidy summary would hide the one diagnostic
            # there is.
            logger.warning(
                "tryon %s: %s pass for slot %s failed: %s",
                garment_set_hash,
                provider,
                garment["slot"],
                exc,
            )
            if passes == 0:
                # Nothing rendered at all: leave no object, so the API keeps
                # answering with the board.
                await _record_failure(uid, garment_set_hash, str(exc))
                return {"rendered": False, "reason": str(exc), "skipped": skipped}
            # A partial chain IS worth keeping — a body wearing the trousers is
            # a better answer than no render, and the response says which
            # garments made it in.
            break
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("tryon %s: unexpected failure: %s", garment_set_hash, exc)
            await _record_failure(uid, garment_set_hash, "render failed")
            return {"rendered": False, "reason": "render failed"}

    # The provider invented a face; give the user theirs back. `original` is
    # the untouched body photo, not the running `current`, which by now has
    # been through one diffusion pass per garment.
    if getattr(settings, "tryon_restore_face", True):
        current = await _restore_head(original, current)

    key = tryon_key(uid, garment_set_hash)
    store.put_bytes(key, current, content_type="image/png")

    rendered_slots = [g["slot"] for g in renderable[:passes]]

    # WHAT THE RENDER DOES NOT COVER IS PART OF THE RESULT.
    #
    # No provider renders footwear -- SLOT_TO_CATEGORY maps upper, lower and
    # dress only -- so the shoes in the output are the ones in the user's own
    # body photo, not the ones in the outfit. That was invisible: the card
    # said "rendered" and a user comparing it with their wardrobe reasonably
    # concluded the render was wrong.
    #
    # Recorded next to the failure rows, for the same reason: the worker is
    # the only place that knows, and the API cannot read arq's result.
    await _record_render(uid, garment_set_hash, rendered_slots, skipped + dropped)

    logger.info(
        "tryon %s: %d/%d passes via %s, skipped=%s",
        garment_set_hash,
        passes,
        len(renderable),
        provider,
        skipped + dropped,
    )
    return {
        "rendered": True,
        "key": key,
        "passes": passes,
        "rendered_slots": rendered_slots,
        # Reported, not silently dropped: the user is looking at a picture of
        # themselves and needs to know it is missing their saree.
        "skipped": skipped + dropped,
        "provider": provider,
    }
