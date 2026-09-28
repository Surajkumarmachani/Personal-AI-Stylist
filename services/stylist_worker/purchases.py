"""Turn a forwarded order email into wardrobe garments (Phase 15).

Queued by the outbox as `purchase_email.received` (routers/purchases.py stored
the email). One model call reads it; see stylist_shop/order_email.py for why a
model, and how its answer is fenced in.

ORDER OF WORK
-------------
Photos are fetched BEFORE any transaction opens: a slow store CDN must not hold
a database connection. Then one tenant transaction writes every garment, any
reversal, and the email's outcome together, so a crash leaves the email
`received` (and the job retryable) rather than half-applied.

ONE PURCHASE, MANY EMAILS
-------------------------
An order sends "confirmed", then "shipped", then "delivered". Each line gets a
`purchase_key` (store + order + title + size), and a key already in the
wardrobe is skipped, so the shirt is added once however many emails mention it.
"cancelled" or "returned" retires what this feature added for that order, but
only if the user has not claimed it since (corrected it or worn it), the same
rule as a merchant-reported return in routers/shop.py.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import text

from stylist_api.settings import get_settings
from stylist_db.session import tenant_session
from stylist_domain.taxonomy import load_taxonomy
from stylist_shop.order_email import (
    PURCHASE_KINDS,
    REVERSAL_KINDS,
    build_messages,
    garment_fields,
    parse_extraction,
    purchase_key,
    read_email,
)
from stylist_shop.own import UnsafeImageURL, fetch_packshot
from stylist_worker.deps import get_litellm_client, get_object_store

logger = logging.getLogger(__name__)

# A 1x1 transparent PNG: `original_key` is NOT NULL, and an item whose photo
# could not be fetched should still be recorded. Same placeholder as shop.py.
PLACEHOLDER_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)

INSERT_GARMENT = text(
    """
    INSERT INTO garments (
        id, user_id, original_key, cutout_key, slot, subcategory,
        primary_colour, dress_code, formality, warmth, brand,
        purchase_price_minor, purchase_currency, attributes_raw,
        field_confidence, user_verified_fields, extractor_version,
        state, needs_review, is_active, moderation, needs_wash,
        created_at, updated_at
    ) VALUES (
        :id, :uid, :okey, :ckey, CAST(:slot AS slot),
        CAST(:subcategory AS subcategory),
        CAST(:primary_colour AS colour),
        CAST(:dress_code AS dress_code), :formality, :warmth, :brand,
        :price, :currency, CAST(:attrs AS jsonb), CAST(:conf AS jsonb),
        '{}', :extractor, 'matted', :review, true,
        '{}'::jsonb, false, now(), now()
    )
    """
)


async def _finish(
    user_id: uuid.UUID, email_id: uuid.UUID, status: str, detail: dict[str, Any]
) -> None:
    async with tenant_session(user_id) as db:
        await db.execute(
            text(
                "UPDATE purchase_email SET status = :s, detail = CAST(:d AS jsonb), "
                "body_html = NULL, body_text = NULL, processed_at = now() WHERE id = :id"
            ),
            {"s": status, "d": json.dumps(detail), "id": email_id},
        )


async def ingest_order_email(
    ctx: dict[str, Any],
    *,
    user_id: str,
    aggregate_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    uid = uuid.UUID(user_id)
    email_id = uuid.UUID(aggregate_id)
    settings = get_settings()

    async with tenant_session(uid) as db:
        row = (
            (
                await db.execute(
                    text(
                        "SELECT status, store, subject, body_html, body_text "
                        "FROM purchase_email WHERE id = :id"
                    ),
                    {"id": email_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        api_key = (await db.execute(text("SELECT litellm_key FROM user_profile LIMIT 1"))).scalar()
    if row is None or row["status"] != "received":
        return {"skipped": True}

    store = row["store"] or "store"
    body, images = read_email(row["body_html"], row["body_text"])
    if not api_key:
        await _finish(uid, email_id, "failed", {"error": "no model key for this account"})
        return {"failed": "no key"}

    taxonomy = load_taxonomy()
    try:
        result = await get_litellm_client().chat(
            model=settings.order_email_model,
            messages=build_messages(
                store=store, subject=row["subject"], text=body, images=images, taxonomy=taxonomy
            ),
            api_key=api_key,
            response_format={"type": "json_object"},
            max_tokens=4096,
        )
        extraction = parse_extraction(result.content, taxonomy=taxonomy, images=images)
    except Exception as exc:
        logger.warning("could not read purchase email %s: %s", email_id, exc)
        await _finish(uid, email_id, "failed", {"error": f"could not read the email: {exc}"[:300]})
        return {"failed": "extraction"}

    detail: dict[str, Any] = {
        "kind": extraction.kind,
        "order_ref": extraction.order_ref,
        "added": [],
        "already_in_wardrobe": [],
        "dropped": list(extraction.dropped),
        "retired": 0,
    }

    # Photos first, outside any transaction.
    fetched: dict[str, tuple[bytes, str] | None] = {}
    if extraction.kind in PURCHASE_KINDS:
        for item in extraction.items:
            if item.image_url and item.image_url not in fetched:
                try:
                    fetched[item.image_url] = await fetch_packshot(item.image_url)
                except UnsafeImageURL:
                    fetched[item.image_url] = None

    store_client = get_object_store()
    async with tenant_session(uid) as db:
        if extraction.kind in PURCHASE_KINDS:
            for item in extraction.items:
                key = purchase_key(store, extraction.order_ref, item)
                exists = (
                    await db.execute(
                        text("SELECT 1 FROM garments WHERE attributes_raw->>'purchase_key' = :k"),
                        {"k": key},
                    )
                ).scalar()
                if exists:
                    detail["already_in_wardrobe"].append(item.title)
                    continue

                fields = garment_fields(
                    item, store=store, order_ref=extraction.order_ref, email_id=str(email_id)
                )
                garment_id = uuid.uuid4()
                original_key = f"originals/{uid}/{garment_id}"
                cutout_key = None
                photo = fetched.get(item.image_url or "")
                if photo is not None:
                    data, content_type = photo
                    store_client.put_bytes(original_key, data, content_type=content_type)
                    # A store photo is already a presentable picture of the
                    # item; it doubles as the cutout, as in the catalogue path.
                    cutout_key = f"cutouts/{uid}/{garment_id}.png"
                    store_client.put_bytes(cutout_key, data, content_type=content_type)
                else:
                    store_client.put_bytes(original_key, PLACEHOLDER_PNG, content_type="image/png")

                await db.execute(
                    INSERT_GARMENT,
                    {
                        "id": garment_id,
                        "uid": str(uid),
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
                    },
                )
                detail["added"].append(
                    {"garment_id": str(garment_id), "title": item.title, "photo": photo is not None}
                )

        elif extraction.kind in REVERSAL_KINDS and extraction.order_ref:
            retired = await db.execute(
                text(
                    """
                    UPDATE garments g SET is_active = false, updated_at = now()
                    WHERE g.attributes_raw->>'confirmed_by' = 'order_email'
                      AND g.attributes_raw->>'store' = :store
                      AND g.attributes_raw->>'order_ref' = :ref
                      AND g.is_active
                      AND g.user_verified_fields = '{}'
                      AND NOT EXISTS (SELECT 1 FROM wear_log w WHERE w.garment_id = g.id)
                    """
                ),
                {"store": store, "ref": extraction.order_ref},
            )
            detail["retired"] = getattr(retired, "rowcount", 0) or 0

        status = "added" if detail["added"] or detail["retired"] else "nothing_to_add"
        await db.execute(
            text(
                "UPDATE purchase_email SET status = :s, detail = CAST(:d AS jsonb), "
                "body_html = NULL, body_text = NULL, processed_at = now() WHERE id = :id"
            ),
            {"s": status, "d": json.dumps(detail), "id": email_id},
        )

    logger.info(
        "purchase email %s: %s added, %s retired",
        email_id,
        len(detail["added"]),
        detail["retired"],
    )
    return {"added": len(detail["added"]), "retired": detail["retired"]}
