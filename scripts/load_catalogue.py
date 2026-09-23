"""Load a CSV catalogue into `product`.

    python scripts/load_catalogue.py catalogue/sample.csv --merchant demo

UPSERT ON (merchant, external_id), which is the merchant's own key. A feed is
re-fetched, not re-created: prices move, stock changes, and a reload that
inserted duplicates would show the same shoe four times and break every
impression count attached to the old row.

ROWS THAT CANNOT BE PLACED IN THE TAXONOMY ARE REJECTED, LOUDLY.
A product with a slot the wardrobe does not have is not a slightly worse row —
no query will ever return it. Storing it would inflate the catalogue while
contributing nothing, and the bug presents much later as "we have thousands of
products and show none".
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from dataclasses import asdict

from sqlalchemy import text

from stylist_db.session import init_engine, system_session
from stylist_shop.providers import CsvProvider, InvalidProduct


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--merchant", default="demo")
    args = ap.parse_args()

    init_engine(os.environ["DATABASE_URL"])
    provider = CsvProvider(args.path, merchant=args.merchant)
    try:
        products = provider.fetch()
    except InvalidProduct as exc:
        print(f"  rejected: {exc}")
        return 1

    # system_session: the catalogue is public content with no tenant, so there
    # is no user context to scope it to (see migration 0022).
    async with system_session() as session:
        for p in products:
            await session.execute(
                text(
                    """
                    INSERT INTO product (
                        id, merchant, external_id, title, brand, slot, subcategory,
                        primary_colour, dress_code, formality, warmth,
                        price_minor, currency, url, image_url, in_stock, updated_at
                    ) VALUES (
                        :id, :merchant, :external_id, :title, :brand,
                        CAST(:slot AS slot), CAST(:subcategory AS subcategory),
                        CAST(:primary_colour AS colour), CAST(:dress_code AS dress_code),
                        :formality, :warmth, :price_minor, :currency, :url,
                        :image_url, :in_stock, now()
                    )
                    -- EVERY column the feed supplies, not just the volatile
                    -- ones. This used to update title, price, stock, url and
                    -- image only, so a re-feed that CORRECTED an attribute
                    -- silently did nothing: two activewear tops were loaded
                    -- with warmth 1, which put them outside the +/-1 band
                    -- tolerance of their own occasion, and re-running the
                    -- loader with warmth 2 changed the CSV and not the row.
                    -- A loader that quietly ignores half its input is worse
                    -- than one that fails, because the file and the database
                    -- disagree while both look fine.
                    ON CONFLICT (merchant, external_id) DO UPDATE SET
                        title = EXCLUDED.title,
                        brand = EXCLUDED.brand,
                        slot = EXCLUDED.slot,
                        subcategory = EXCLUDED.subcategory,
                        primary_colour = EXCLUDED.primary_colour,
                        dress_code = EXCLUDED.dress_code,
                        formality = EXCLUDED.formality,
                        warmth = EXCLUDED.warmth,
                        price_minor = EXCLUDED.price_minor,
                        currency = EXCLUDED.currency,
                        in_stock = EXCLUDED.in_stock,
                        url = EXCLUDED.url,
                        image_url = EXCLUDED.image_url,
                        updated_at = now()
                    """
                ),
                # `asdict`, not `__dict__`: Product uses slots=True, so
                # there is no instance dict to unpack.
                {"id": uuid.uuid4(), **asdict(p)},
            )

    print(f"  loaded {len(products)} products from {args.merchant}")
    return 0


sys.exit(asyncio.run(main()))
