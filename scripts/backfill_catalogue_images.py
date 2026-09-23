"""Give an image to garments added from the catalogue before it had one.

WHY THIS EXISTS AT ALL
----------------------
`POST /shop/own/{id}` copies the product photo into the user's wardrobe at the
moment they confirm the purchase. A garment confirmed while the catalogue had
no `image_url` therefore has none, and re-tapping cannot fix it: the endpoint
is idempotent by design, so it returns the existing garment rather than
rebuilding it. That is the correct behaviour for a double-tap and the wrong
behaviour here, which is what makes this a separate, explicit job.

WHAT IT WILL NOT DO
-------------------
It only fills garments whose `cutout_key` IS NULL. A garment that already has
an image has either been photographed by the user or filled by an earlier run,
and a user's own photograph of their own kurta is better evidence than a stock
photo of some kurta. Overwriting that would be a downgrade.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from stylist_api.settings import get_settings
from stylist_clients.storage import ObjectStore
from stylist_shop.own import (
    ALLOWED_IMAGE_TYPES,
    MAX_IMAGE_BYTES,
    UnsafeImageURL,
    check_image_url,
)

SQL = sa.text(
    """
    SELECT g.id, g.user_id, p.image_url, p.title
    FROM garments g
    JOIN product p ON p.id = g.sourced_product_id
    WHERE g.cutout_key IS NULL
      AND p.image_url IS NOT NULL
      AND p.image_url <> ''
    """
)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set")
        return 1

    st = get_settings()
    store = ObjectStore(
        bucket=st.s3_bucket,
        endpoint_url=st.s3_endpoint_url,
        region=st.s3_region,
        access_key=st.s3_access_key,
        secret_key=st.s3_secret_key,
    )

    engine = create_async_engine(dsn)
    filled = skipped = 0
    async with engine.begin() as conn:
        rows = (await conn.execute(SQL)).all()
        print(f"{len(rows)} garment(s) missing an image")
        for garment_id, user_id, image_url, title in rows:
            try:
                check_image_url(image_url)
            except UnsafeImageURL as exc:
                print(f"  {title:28} refused: {exc}")
                skipped += 1
                continue
            try:
                r = httpx.get(image_url, timeout=30, follow_redirects=False)
                r.raise_for_status()
            except httpx.HTTPError as exc:
                print(f"  {title:28} fetch failed: {exc}")
                skipped += 1
                continue

            content_type = r.headers.get("content-type", "").split(";")[0].strip()
            if content_type not in ALLOWED_IMAGE_TYPES or len(r.content) > MAX_IMAGE_BYTES:
                print(f"  {title:28} rejected: {content_type}, {len(r.content)} bytes")
                skipped += 1
                continue

            if args.dry_run:
                print(f"  {title:28} would fill ({len(r.content)} bytes)")
                filled += 1
                continue

            # Same prefixes as the endpoint, so the erasure saga still reaches
            # these objects by its existing prefix list.
            key = f"cutouts/{user_id}/{garment_id}.png"
            store.put_bytes(key, r.content, content_type=content_type)
            await conn.execute(
                sa.text(
                    "UPDATE garments SET cutout_key = :k, updated_at = now() WHERE id = :i"
                ),
                {"k": key, "i": garment_id},
            )
            print(f"  {title:28} filled ({len(r.content)} bytes)")
            filled += 1

    await engine.dispose()
    print(f"\nfilled {filled}, skipped {skipped}{' (dry run)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
