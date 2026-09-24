"""Fill a gap the wardrobe cannot (Phase 13).

ONE QUESTION, NOT A FEED
------------------------
This endpoint answers "your wardrobe cannot dress this occasion — what one
item would fix it?". It does not answer "what should I buy". The difference is
the whole design: a gap is identified by the SAME candidate pool that produced
the outfits on screen, so the suggestion is a consequence of the user trying
to get dressed rather than something the app volunteered.

Putting a shopping feed next to "every look is built from clothes you own"
would undermine the only claim this product has that a retailer does not.

DISCLOSURE IS A FIELD, NOT A FOOTER
-----------------------------------
`affiliate` is returned per response and the UI is required to render it.
Paid-link disclosure is a legal obligation (ASA in the UK, FTC in the US) and
"we put it in the terms" is not compliance. Returning it as data rather than
leaving it to the client means a new surface cannot forget it.

IMPRESSIONS ARE RECORDED, NOT JUST CLICKS
-----------------------------------------
An affiliate programme pays on clicks, so clicks are what a naive
implementation records. A click count with no denominator cannot answer
whether a suggestion was any good — the same missing-denominator mistake this
codebase has already made once with the rationale cache.
"""

from __future__ import annotations

import hmac
import json
import logging
import uuid
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import text

from stylist_api.deps import (
    CacheRedisDep,
    CurrentUser,
    LiteLLMDep,
    ObjectStoreDep,
    SettingsDep,
    TenantDB,
)
from stylist_api.routers.dresses_as import read_dresses_as
from stylist_db.session import system_session, tenant_session
from stylist_domain.context import resolve_context
from stylist_domain.taxonomy import load_taxonomy
from stylist_shop.conversions import normalise_status, tracked_url
from stylist_shop.gaps import find_gaps
from stylist_shop.own import (
    ALLOWED_IMAGE_TYPES,
    MAX_IMAGE_BYTES,
    UnsafeImageURL,
    check_image_url,
    garment_from_product,
)
from stylist_suggest import load_wardrobe

logger = logging.getLogger(__name__)

router = APIRouter(tags=["shop"])

# Few enough to be a decision, not a catalogue. The screen is answering "what
# would fix this", and twenty options is not an answer.
MAX_SUGGESTIONS = 4

# Shown verbatim. See the module docstring for why this is a field.
AFFILIATE_NOTICE = (
    "These are shop links. We may earn a commission if you buy — it costs you "
    "nothing extra, and it never changes what we suggest from your own wardrobe."
)


@router.get("/shop/gaps")
async def shop_gaps(
    user: CurrentUser,
    db: TenantDB,
    store: ObjectStoreDep,
    settings: SettingsDep,
    gateway: LiteLLMDep,
    cache: CacheRedisDep,
    occasion: Annotated[str, Query()] = "casual_outing",
    feels_like_c: Annotated[float | None, Query(ge=-30, le=60)] = None,
) -> dict[str, Any]:
    """What is missing for this occasion, and what would fill it."""
    taxonomy = load_taxonomy()
    if occasion not in {o["id"] for o in taxonomy.raw["occasions"]}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="unknown occasion"
        )

    ctx = resolve_context(
        occasion=occasion,
        feels_like_c=26.0 if feels_like_c is None else feels_like_c,
    )
    # The SAME pool the suggestions come from — see `find_gaps`.
    pool = await load_wardrobe(db, ctx)
    gaps = find_gaps(pool, ctx)

    auto_add = bool(settings.shop_postback_secret)
    # WHOSE CLOTHES. An empty wardrobe used to be offered a lehenga skirt and
    # a sherwani side by side. Unasked (None) and 'all' show everything.
    dresses_as = await read_dresses_as(db)
    genders = (
        [dresses_as, "unisex"] if dresses_as in {"women", "men"} else ["women", "men", "unisex"]
    )
    results: list[dict[str, Any]] = []
    for gap in gaps:
        rows = await db.execute(
            text(
                """
                SELECT id, merchant, title, brand, slot::text AS slot,
                       subcategory::text AS subcategory,
                       primary_colour::text AS primary_colour,
                       dress_code::text AS dress_code,
                       price_minor, currency, url, image_url
                FROM product
                WHERE in_stock
                  AND slot::text = :slot
                  -- The dress code is the point. A `feet` gap for an
                  -- interview is not filled by trainers, and returning them
                  -- would make the feature look like an untargeted ad.
                  AND (dress_code IS NULL OR dress_code::text = ANY(:codes))
                  AND (warmth IS NULL OR abs(warmth - :warmth) <= 1)
                  -- NULL is unisex: an unplaced product is shown to everyone
                  -- rather than hidden from everyone. See migration 0026.
                  AND (gender IS NULL OR gender = ANY(:genders))
                ORDER BY
                  -- Cheapest first among equally suitable items. Not a
                  -- margin-maximising order: the user is being asked to spend
                  -- money to fix a gap, and the honest default is the least
                  -- that solves it.
                  price_minor NULLS LAST
                LIMIT :lim
                """
            ),
            {
                "slot": gap.slot,
                "codes": list(
                    taxonomy.dress_code_compatibility.get(
                        gap.dress_code or "", [gap.dress_code]
                    )
                )
                or [gap.dress_code],
                "warmth": gap.warmth_target or 3,
                "lim": MAX_SUGGESTIONS,
                "genders": genders,
            },
        )
        products = [dict(r) for r in rows.mappings()]
        for p in products:
            p["id"] = str(p["id"])

        # Impressions, in the same request that produced them. Recorded even
        # when the list is empty is NOT useful, so only real shows are logged.
        for p in products:
            event_id = uuid.uuid4()
            await db.execute(
                text(
                    "INSERT INTO product_event (id, user_id, product_id, kind, occasion) "
                    "VALUES (:id, CAST(:uid AS uuid), CAST(:pid AS uuid), 'shown', :occ)"
                ),
                {
                    "id": event_id,
                    "uid": str(user.id),
                    "pid": p["id"],
                    "occ": occasion,
                },
            )
            # THE IMPRESSION IS THE AFFILIATE REFERENCE. Its id goes on the
            # link, the network echoes it back in the postback, and
            # `shop_ref_owner` turns it into this user and this product. Put
            # on the link HERE rather than by `/shop/click`, because the click
            # is recorded alongside the navigation, not before it — see
            # `record_click` for why our server stays out of that path.
            if auto_add:
                p["url"] = tracked_url(p["url"], str(event_id), settings.shop_subid_param)

        # A GAP WITH NOTHING TO OFFER IS NOT ACTIONABLE.
        #
        # `find_gaps` states every route to a base outfit because it cannot
        # see the catalogue; this is where the catalogue answers. Showing
        # "no full body — 0 suggestions" next to a route we CAN fill tells
        # the user about a hole in our stock, not about their wardrobe.
        if not products:
            continue

        results.append(
            {
                "slot": gap.slot,
                "severity": gap.severity,
                "reason": gap.reason,
                "products": products,
            }
        )

    return {
        "occasion": occasion,
        "gaps": results,
        # Rendered verbatim by every client. See the module docstring.
        "affiliate": AFFILIATE_NOTICE,
        # Stated so a client does not have to infer "nothing to buy" from an
        # empty list, which is also what an unstocked catalogue looks like.
        # True when gaps EXIST but nothing in the catalogue fills them —
        # distinct from "your wardrobe is fine", which is `gaps == []`.
        "catalogue_empty": bool(gaps) and not results,
        # True when a purchase through these links is reported back by the
        # merchant and added to the wardrobe without the user saying so. The
        # client still offers "I bought this": reports lag the order by hours.
        "auto_add": auto_add,
    }


@router.post("/shop/click/{product_id}")
async def record_click(
    product_id: uuid.UUID, user: CurrentUser, db: TenantDB
) -> dict[str, Any]:
    """Attribution. Returns the destination rather than redirecting.

    A 302 here would be the conventional affiliate pattern and is worse for
    this app: it puts our server in the path of every outbound click, so an
    outage or a slow response becomes a broken link to the merchant. The
    client already has the url from `/shop/gaps`; this only records that the
    click happened.
    """
    row = await db.execute(
        text("SELECT url FROM product WHERE id = :id"), {"id": product_id}
    )
    url = row.scalar_one_or_none()
    if url is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown product")

    await db.execute(
        text(
            "INSERT INTO product_event (id, user_id, product_id, kind) "
            "VALUES (:id, CAST(:uid AS uuid), :pid, 'clicked')"
        ),
        {"id": uuid.uuid4(), "uid": str(user.id), "pid": product_id},
    )
    return {"url": url, "recorded": True}


async def _fetch_packshot(url: str) -> tuple[bytes, str] | None:
    """Download the merchant's product image, or return None.

    Returns None rather than raising for an image that is merely missing or
    broken: a garment the user genuinely bought should still be added to their
    wardrobe when the merchant's CDN is having a bad day. Only an UNSAFE url is
    an error, because that is a request we must refuse to make at all.
    """
    check_image_url(url)  # raises UnsafeImageURL; see stylist_shop.own
    try:
        async with (
            httpx.AsyncClient(
                timeout=10.0,
                # A redirect can walk a public hostname to a private one,
                # which is precisely what check_image_url exists to prevent.
                follow_redirects=False,
            ) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                return None
            content_type = response.headers.get("content-type", "").split(";")[0].strip()
            if content_type not in ALLOWED_IMAGE_TYPES:
                return None
            body = bytearray()
            # Streamed with a running cap. `content-length` is a claim by the
            # remote server, not a guarantee, so the bound has to be enforced
            # on the bytes actually received.
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_IMAGE_BYTES:
                    return None
            return bytes(body), content_type
    except httpx.HTTPError:
        return None


@router.post("/shop/own/{product_id}", status_code=status.HTTP_201_CREATED)
async def own_product(
    product_id: uuid.UUID,
    user: CurrentUser,
    db: TenantDB,
    store: ObjectStoreDep,
) -> dict[str, Any]:
    """"I bought this" — add the product to the wardrobe.

    STILL NEEDED NOW THAT PURCHASES ARE REPORTED
    --------------------------------------------
    `/shop/conversions` adds the garment when the merchant reports the order,
    but only for a deployment with an affiliate postback configured, and only
    once the network sends it — hours after checkout on most of them. This is
    the path for everything else: an unconfigured deployment, a network with
    no postback, a purchase made in a shop. Both call `_add_to_wardrobe`, and
    the row records which one it was.
    """
    return await _add_to_wardrobe(db, store, user.id, product_id, confirmed_by="user")


async def _add_to_wardrobe(
    db: Any,
    store: Any,
    user_id: uuid.UUID,
    product_id: uuid.UUID,
    *,
    confirmed_by: str,
) -> dict[str, Any]:
    """Create the garment for a bought product. Idempotent per (user, product).

    NO PIPELINE RUN
    ---------------
    The merchant already told us the slot, colour, dress code, formality and
    warmth. Segmenting and tagging a packshot to re-derive them would cost
    money to produce a worse answer. See stylist_shop.own.
    """
    row = await db.execute(
        text(
            "SELECT id, merchant, external_id, title, brand, slot::text AS slot, "
            "subcategory::text AS subcategory, primary_colour::text AS primary_colour, "
            "dress_code::text AS dress_code, formality, warmth, price_minor, "
            "currency, image_url FROM product WHERE id = :id"
        ),
        {"id": product_id},
    )
    product = row.mappings().one_or_none()
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown product")

    fields = garment_from_product(dict(product), confirmed_by=confirmed_by)
    garment_id = uuid.uuid4()

    # `originals/` and `cutouts/`, deliberately reusing the existing prefixes
    # rather than inventing a `catalogue/` one: the erasure saga deletes a
    # user's objects by prefix list, and a new prefix that is not added there
    # is an object that survives account deletion.
    original_key = f"originals/{user_id}/{garment_id}"
    cutout_key: str | None = None

    image_url = product["image_url"]
    packshot = None
    if image_url:
        try:
            packshot = await _fetch_packshot(image_url)
        except UnsafeImageURL as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"refusing to fetch that image: {exc}",
            ) from exc

    if packshot is not None:
        body, content_type = packshot
        store.put_bytes(original_key, body, content_type=content_type)
        # A packshot is already isolated on a plain background, so it doubles
        # as the cutout. Running the matting model over it would spend GPU
        # time to approximate what the merchant's photographer already did.
        cutout_key = f"cutouts/{user_id}/{garment_id}.png"
        store.put_bytes(cutout_key, body, content_type=content_type)
    else:
        # NO IMAGE, AND WE DO NOT INVENT ONE.
        #
        # `original_key` is NOT NULL, so something must be stored; a 1x1
        # transparent PNG is a placeholder that is obviously not a photograph.
        # `cutout_key` stays NULL, which is what the wardrobe grid already
        # renders as a placeholder tile — so a garment with no picture LOOKS
        # like a garment with no picture, instead of like a broken image.
        store.put_bytes(
            original_key,
            bytes.fromhex(
                "89504e470d0a1a0a0000000d494844520000000100000001080600000"
                "01f15c4890000000a49444154789c6300010000050001"
                "0d0a2db40000000049454e44ae426082"
            ),
            content_type="image/png",
        )

    # ON CONFLICT rather than catching IntegrityError: a failed INSERT aborts
    # the surrounding transaction, so the recovery SELECT could not run inside
    # it -- measured, as `InFailedSQLTransactionError`, on the second tap.
    # Letting Postgres resolve the conflict keeps one round trip and one
    # transaction, and is race-free against a concurrent duplicate — which is
    # exactly what a network retrying a postback while the user taps "I
    # bought this" produces.
    inserted = await db.execute(
        text(
            """
                INSERT INTO garments (
                    id, user_id, original_key, cutout_key, slot, subcategory,
                    primary_colour, dress_code, formality, warmth, brand,
                    purchase_price_minor, purchase_currency, attributes_raw,
                    field_confidence, user_verified_fields, extractor_version,
                    state, needs_review, is_active, moderation, needs_wash,
                    sourced_product_id, created_at, updated_at
                ) VALUES (
                    :id, :uid, :okey, :ckey, CAST(:slot AS slot),
                    CAST(:subcategory AS subcategory),
                    CAST(:primary_colour AS colour),
                    CAST(:dress_code AS dress_code), :formality, :warmth, :brand,
                    :price, :currency, CAST(:attrs AS jsonb), CAST(:conf AS jsonb),
                    '{}', :extractor, 'matted', :review, true,
                    '{}'::jsonb, false, :pid, now(), now()
                )
                ON CONFLICT (user_id, sourced_product_id)
                    WHERE sourced_product_id IS NOT NULL
                    DO NOTHING
                RETURNING id
                """
        ),
        {
            "id": garment_id,
            "uid": str(user_id),
            "okey": original_key,
            "ckey": cutout_key,
            "slot": fields["slot"],
            "subcategory": fields["subcategory"],
            "primary_colour": fields["primary_colour"],
            "dress_code": fields["dress_code"],
            "formality": fields["formality"],
            "warmth": fields["warmth"],
            "brand": fields["brand"],
            "price": fields["purchase_price_minor"],
            "currency": fields["purchase_currency"],
            "attrs": json.dumps(fields["attributes_raw"]),
            "conf": json.dumps(fields["field_confidence"]),
            "extractor": fields["extractor_version"],
            "review": fields["needs_review"],
            "pid": str(product_id),
        },
    )

    if inserted.scalar_one_or_none() is None:
        # Already owned. Tapping twice, or a conversion feed replaying a click,
        # must not put two copies of the same shoes in a wardrobe -- and every
        # recommendation is built from this wardrobe, so a phantom duplicate
        # would quietly skew them. Idempotent, not an error.
        existing = await db.execute(
            text(
                "SELECT id FROM garments WHERE user_id = CAST(:uid AS uuid) "
                "AND sourced_product_id = :pid"
            ),
            {"uid": str(user_id), "pid": str(product_id)},
        )
        return {
            "garment_id": str(existing.scalar_one()),
            "created": False,
            "has_image": False,
            "note": "already in your wardrobe",
        }

    await db.execute(
        text(
            "INSERT INTO product_event (id, user_id, product_id, kind) "
            "VALUES (:id, CAST(:uid AS uuid), CAST(:pid AS uuid), 'bought')"
        ),
        {"id": uuid.uuid4(), "uid": str(user_id), "pid": str(product_id)},
    )

    return {
        "garment_id": str(garment_id),
        "created": True,
        "has_image": cutout_key is not None,
        # Said plainly so the client does not have to guess why a tile is
        # blank. This is the honest consequence of a catalogue with no images.
        "note": (
            "Added from the catalogue. Tags came from the merchant, so check them."
            if cutout_key
            else "Added, but the merchant listing has no photo — "
            "take one and it will show in your wardrobe."
        ),
    }


@router.get("/shop/conversions", operation_id="shop_conversion_postback_get")
@router.post("/shop/conversions", operation_id="shop_conversion_postback_post")
async def conversion_postback(
    settings: SettingsDep,
    store: ObjectStoreDep,
    secret: Annotated[str, Query()] = "",
    ref: Annotated[str, Query()] = "",
    conversion_id: Annotated[str, Query(max_length=128)] = "",
    order_status: Annotated[str, Query(alias="status")] = "",
    network: Annotated[str, Query(max_length=40)] = "affiliate",
) -> dict[str, Any]:
    """A merchant reports an order made through one of our links.

    Registered with the affiliate network as a postback template, e.g.
    `/shop/conversions?secret=…&ref={subid}&conversion_id={order_id}&status={status}`.
    GET and POST both accepted with the fields in the query string, because
    networks differ on the verb and all of them can template a URL.

    NO USER TOKEN, SO THE SECRET IS THE ONLY AUTHORITY. Compared in constant
    time, and an unset secret refuses everything — see `shop_postback_secret`.

    2xx FOR A REPORT WE WILL NEVER ACT ON. A network retries anything else for
    days. A reference that matches no impression (expired, or someone else's
    catalogue) will not start matching on the fifth attempt, so it is
    acknowledged and logged rather than retried into the logs forever.
    """
    configured = settings.shop_postback_secret
    if not configured or not hmac.compare_digest(secret.encode(), configured.encode()):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bad secret")

    state = normalise_status(order_status)
    if state is None:
        # Refused, not guessed. Reading an unknown spelling as "approved" puts
        # a garment in a wardrobe; a 400 makes the network show the error to
        # whoever configured the postback, which is who can fix it.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"unknown status {order_status!r}"
        )
    if not conversion_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="conversion_id missing")
    try:
        ref_id = uuid.UUID(ref)
    except ValueError:
        return {"accepted": False, "reason": "unknown ref"}

    # The one privileged read: reference -> (user, product). See 0025.
    async with system_session() as sys_db:
        owner = (
            await sys_db.execute(
                text("SELECT user_id, product_id FROM shop_ref_owner(:ref)"), {"ref": ref_id}
            )
        ).one_or_none()
    if owner is None:
        logger.info("shop postback for unknown ref %s from %s", ref_id, network)
        return {"accepted": False, "reason": "unknown ref"}
    user_id, product_id = owner

    async with tenant_session(user_id) as db:
        row = (
            await db.execute(
                text(
                    """
                    INSERT INTO shop_conversion
                        (id, user_id, product_id, ref, network, conversion_id, status, raw)
                    VALUES (:id, :uid, :pid, :ref, :net, :cid, :status, CAST(:raw AS jsonb))
                    ON CONFLICT (network, conversion_id) DO UPDATE
                        SET status = EXCLUDED.status, raw = EXCLUDED.raw, updated_at = now()
                    RETURNING id, garment_id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "uid": user_id,
                    "pid": product_id,
                    "ref": ref_id,
                    "net": network,
                    "cid": conversion_id,
                    "status": state,
                    # The secret is deliberately NOT stored with the rest.
                    "raw": json.dumps({"status": order_status}),
                },
            )
        ).one()
        conversion_row_id, garment_id = row

        if state == "rejected":
            # A cancellation or a return: they no longer own it. Retired only
            # if the MERCHANT put it there and the user has since done nothing
            # to claim it — a garment they tapped "I bought this" on, corrected
            # the tags of, or wore, is theirs to remove, not ours.
            if garment_id is not None:
                await db.execute(
                    text(
                        """
                        UPDATE garments SET is_active = false, updated_at = now()
                        WHERE id = :gid
                          AND attributes_raw->>'confirmed_by' = 'conversion_feed'
                          AND user_verified_fields = '{}'
                          AND NOT EXISTS (SELECT 1 FROM wear_log w WHERE w.garment_id = :gid)
                        """
                    ),
                    {"gid": garment_id},
                )
            return {"accepted": True, "status": state, "garment_id": None}

        if product_id is None:
            # The product left the catalogue between the click and the report.
            # The order is recorded; there is nothing left to copy tags from.
            return {"accepted": True, "status": state, "garment_id": None}

        if garment_id is None:
            added = await _add_to_wardrobe(
                db, store, user_id, product_id, confirmed_by="conversion_feed"
            )
            garment_id = uuid.UUID(added["garment_id"])
            await db.execute(
                text("UPDATE shop_conversion SET garment_id = :gid WHERE id = :id"),
                {"gid": garment_id, "id": conversion_row_id},
            )

        # A return reversed, or a reorder after a return: the uniqueness index
        # hands back the retired garment, so it is brought back rather than
        # left hidden. Merchant-added rows only, for the reason above.
        await db.execute(
            text(
                "UPDATE garments SET is_active = true, updated_at = now() "
                "WHERE id = :gid AND NOT is_active "
                "AND attributes_raw->>'confirmed_by' = 'conversion_feed'"
            ),
            {"gid": garment_id},
        )
    return {"accepted": True, "status": state, "garment_id": str(garment_id)}
