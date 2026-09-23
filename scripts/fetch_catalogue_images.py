"""Put a photograph against each catalogue product.

WHAT THESE IMAGES ARE, AND ARE NOT
-----------------------------------
They are ILLUSTRATIVE STOCK PHOTOGRAPHS, not merchant packshots. The demo
catalogue's `url` column points at Myntra SEARCH pages, not at one specific
listing, so there is no particular product whose photo this could be. A photo
of some white sneakers standing in for "White leather sneakers" is honest for a
demo row and would be a misrepresentation for a real one.

So this script refuses to touch any merchant but `demo`. When a real catalogue
arrives, its image_url comes from the merchant's own feed — that is the only
image that can truthfully be captioned with the merchant's price.

WHY REMOTE URLS AND NOT DOWNLOADED FILES
-----------------------------------------
`fetch_occasion_images.py` downloads, because those tiles are part of the app's
own chrome and must not break when a third party moves a file. A product image
is different: `POST /shop/own/{id}` fetches `image_url` server-side at the
moment the user says they bought something, and stores a COPY in their
wardrobe. So the URL only has to survive until then, and storing a second copy
of every catalogue image for products nobody buys is waste.

Unsplash is used because the licence permits this and the project already has
a key. Attribution is not legally required by that licence but IS requested,
so it is written to catalogue/CREDITS.md rather than left implicit.
"""

from __future__ import annotations

import argparse
import csv
import os
import pathlib
import re
import sys

import httpx

API = "https://api.unsplash.com"
CATALOGUE = pathlib.Path("catalogue")

# One query per product. Deliberately describes the GARMENT, not a person
# wearing it: this image sits in a small square tile next to a price, and a
# full-length fashion shot is unreadable at that size.
QUERIES: dict[str, str] = {
    "demo-oxford-black": "black leather oxford dress shoes",
    "demo-derby-brown": "brown leather derby shoes",
    "demo-sneaker-white": "white leather sneakers",
    # SHORT, deliberately. "golden mojari jutti indian footwear" and
    # "kolhapuri chappal leather sandal" both returned ZERO results: Unsplash
    # narrows on every extra term, and the long phrases described the product
    # perfectly while matching nothing. "mojari" alone returns 161. "kolhapuri"
    # returns 0 at any length -- the photographs simply are not there -- so
    # that one is described by what it is rather than by its regional name.
    "demo-mojari-gold": "mojari",
    "demo-kolhapuri-tan": "handmade leather sandal",
    "demo-loafer-navy": "navy suede loafers",
    "demo-chinos-beige": "beige chino trousers folded",
    "demo-trousers-charcoal": "charcoal formal trousers",
    "demo-kurta-white": "white cotton kurta indian menswear",
    "demo-shirt-oxford-blue": "light blue oxford shirt",
    # Ethnic bottoms and full-body pieces. Without these, every wedding and
    # festival gap was unfillable, so the panel hid itself entirely -- the
    # wardrobe was short a piece and we had nothing to offer for it.
    #
    # SEVERAL INDIAN GARMENT NAMES RETURN NOTHING AT ALL on Unsplash --
    # "churidar", "sherwani" and "anarkali" each give ZERO results at any
    # phrasing, while "indian traditional dress" gives 70. The library is
    # indexed in English generic terms, not in the vocabulary this taxonomy
    # uses, so these queries describe the garment rather than name it. That
    # is a limitation of the stock library, and one more reason a real
    # merchant feed is the right source for a real catalogue.
    "demo-churidar-cream": "indian trousers",
    "demo-pyjama-ivory": "kurta pyjama",
    "demo-lehenga-maroon": "lehenga",
    "demo-palazzo-gold": "palazzo pants",
    "demo-sherwani-ivory": "indian groom",
    "demo-anarkali-teal": "indian ethnic dress",
    "demo-salwar-set-pink": "salwar kameez",
    "demo-indowestern-black": "indian wedding outfit",
    # Activewear. The gym occasion had NO stock at all, which is why it could
    # only offer what the wardrobe already held -- jeans.
    "demo-jersey-navy": "sports t shirt",
    "demo-tank-grey": "gym tank top",
    "demo-joggers-black": "joggers",
    "demo-shorts-navy": "running shorts",
    "demo-leggings-black": "gym leggings",
    "demo-running-white": "running shoes",
}


def read_key() -> str:
    key = os.environ.get("UNSPLASH_ACCESS_KEY", "").strip()
    if not key:
        env = pathlib.Path(".env")
        if env.exists():
            m = re.search(r"^UNSPLASH_ACCESS_KEY=(.+)$", env.read_text(), re.M)
            key = m.group(1).strip() if m else ""
    if not key:
        print("UNSPLASH_ACCESS_KEY is not set (env or .env)")
        raise SystemExit(1)
    return key


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(CATALOGUE / "sample.csv"))
    ap.add_argument("--dry-run", action="store_true", help="search and report; write nothing")
    args = ap.parse_args()

    path = pathlib.Path(args.csv)
    rows = list(csv.DictReader(path.open()))
    if not rows:
        print(f"no rows in {path}")
        return 1
    if any(r.get("external_id", "").split("-")[0] != "demo" for r in rows):
        print("refusing: this writes illustrative stock photos and only the demo")
        print("catalogue may carry those. A real merchant's image_url comes from")
        print("its own feed -- see the module docstring.")
        return 1

    headers = {"Authorization": f"Client-ID {read_key()}", "Accept-Version": "v1"}
    credits: list[tuple[str, str, str, str]] = []
    # The same photo can win two queries, and two identical tiles in one gap
    # panel read as a bug rather than a coincidence.
    used: set[str] = set()

    for row in rows:
        ext = row["external_id"]
        query = QUERIES.get(ext)
        if not query:
            print(f"  {ext:24} no query defined, left alone")
            continue
        try:
            r = httpx.get(
                f"{API}/search/photos",
                params={
                    "query": query,
                    "per_page": 8,
                    # Square-ish. The tile is 1:1, and a portrait crop of a
                    # shoe throws away the shoe.
                    "orientation": "squarish",
                    "content_filter": "high",
                },
                headers=headers,
                timeout=40,
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"  {ext:24} search failed: {exc}")
            continue

        hits = [h for h in (r.json().get("results") or []) if h["id"] not in used]
        if not hits:
            print(f"  {ext:24} no unused results for {query!r}")
            continue
        photo = hits[0]
        used.add(photo["id"])

        # `small` (~400px): the tile renders ~200px, so this covers 2x and
        # nothing more. `regular` would be four times the bytes for no gain,
        # fetched server-side on every purchase confirmation.
        url = photo["urls"]["small"]
        print(f"  {ext:24} {photo['id']}  by {photo['user']['name']}")
        if args.dry_run:
            continue
        row["image_url"] = url
        credits.append(
            (ext, photo["user"]["name"], photo["user"]["links"]["html"], photo["links"]["html"])
        )

    if args.dry_run:
        print("\ndry run: nothing written")
        return 0

    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n  wrote {path}")

    lines = [
        "# Catalogue product images — attribution",
        "",
        "Generated by `scripts/fetch_catalogue_images.py`. Do not edit by hand.",
        "",
        "**These are illustrative stock photographs, not merchant packshots.**",
        "The demo catalogue links to search pages rather than to one listing, so",
        "no specific product's photo exists. A real merchant's `image_url` must",
        "come from its own feed.",
        "",
        "Photographs from [Unsplash](https://unsplash.com) under the",
        "[Unsplash License](https://unsplash.com/license). Attribution is not",
        "legally required by that licence but is requested by it.",
        "",
        "| product | photographer | photo |",
        "|---|---|---|",
    ]
    lines += [f"| {e} | [{n}]({u}) | [link]({p}) |" for e, n, u, p in sorted(credits)]
    (CATALOGUE / "CREDITS.md").write_text("\n".join(lines) + "\n")
    print(f"  wrote {CATALOGUE / 'CREDITS.md'} with {len(credits)} attributions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
