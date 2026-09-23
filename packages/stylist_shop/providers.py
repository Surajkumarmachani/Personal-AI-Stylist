"""Where catalogue rows come from.

MERCHANT-AGNOSTIC ON PURPOSE
----------------------------
Amazon PA-API, Flipkart, Myntra-via-an-affiliate-network and a plain CSV all
differ in auth, pagination, rate limits and field names, and every one of them
will change. What does NOT change is what this system needs from a product:
enough taxonomy to match it against a wardrobe gap, a price, a link, a picture.

So the contract is `fetch() -> list[Product]` and nothing above it knows which
merchant answered. Swapping providers must not require a migration or a change
to the matcher.

THE TAXONOMY IS THE INTEGRATION CONTRACT
----------------------------------------
A product that cannot be described as (slot, dress_code, warmth) cannot be
matched to "your wardrobe has no formal footwear for Tuesday". Every adapter
is therefore responsible for mapping its merchant's categories onto this
project's enums, and `validate` rejects what it cannot place rather than
storing a row the matcher will silently never return.

NO CREDENTIALS ARE REQUIRED TO RUN THIS
---------------------------------------
`CsvProvider` exists so the whole feature — matching, ranking, click tracking,
the UI — works end to end today, on a catalogue you control. A real affiliate
account changes one line of configuration, not the design.
"""

from __future__ import annotations

import csv
import pathlib
from dataclasses import dataclass
from typing import Any, Protocol

from stylist_domain.taxonomy import load_taxonomy


@dataclass(frozen=True, slots=True)
class Product:
    merchant: str
    external_id: str
    title: str
    url: str
    slot: str
    brand: str | None = None
    subcategory: str | None = None
    primary_colour: str | None = None
    dress_code: str | None = None
    formality: int | None = None
    warmth: int | None = None
    price_minor: int | None = None
    currency: str | None = None
    image_url: str | None = None
    in_stock: bool = True


class CatalogueProvider(Protocol):
    """One merchant's feed."""

    name: str

    def fetch(self) -> list[Product]: ...


class InvalidProduct(ValueError):  # noqa: N818 - a rejected ROW, not an error type
    """A row that cannot be placed in the taxonomy, and so can never match."""


def validate(product: Product) -> Product:
    """Reject what the matcher could never return.

    A product with a slot outside the taxonomy is not a slightly worse row, it
    is a row that no query will ever select — it would sit in the table
    inflating the catalogue size while contributing nothing, and the bug would
    present as "we have 40,000 products and show none".
    """
    taxonomy = load_taxonomy()
    if product.slot not in taxonomy.slots:
        raise InvalidProduct(f"slot {product.slot!r} is not in the taxonomy")
    for field, allowed in (
        ("subcategory", taxonomy.subcategories),
        ("primary_colour", taxonomy.colours),
        ("dress_code", taxonomy.dress_codes),
    ):
        value = getattr(product, field)
        if value is not None and value not in allowed:
            raise InvalidProduct(f"{field} {value!r} is not in the taxonomy")
    if not product.url:
        raise InvalidProduct("a product with no link cannot be acted on")
    return product


class CsvProvider:
    """A catalogue from a file you control.

    The point is not that CSV is the destination — it is that the feature must
    be buildable, demoable and testable without an affiliate account, so that
    "we are waiting on a merchant" never blocks the half of the work that has
    nothing to do with merchants.
    """

    name = "csv"

    def __init__(self, path: str | pathlib.Path, merchant: str = "csv") -> None:
        self._path = pathlib.Path(path)
        self.name = merchant

    def fetch(self) -> list[Product]:
        rows: list[Product] = []
        with self._path.open(newline="", encoding="utf-8") as handle:
            for raw in csv.DictReader(handle):
                rows.append(validate(_from_row(raw, self.name)))
        return rows


def _int_or_none(value: Any) -> int | None:
    text = str(value or "").strip()
    return int(text) if text else None


def _from_row(raw: dict[str, Any], merchant: str) -> Product:
    def get(key: str) -> str | None:
        value = str(raw.get(key) or "").strip()
        return value or None

    slot = get("slot")
    if not slot:
        raise InvalidProduct("slot is required")
    return Product(
        merchant=merchant,
        external_id=str(raw.get("external_id") or "").strip(),
        title=str(raw.get("title") or "").strip(),
        url=str(raw.get("url") or "").strip(),
        slot=slot,
        brand=get("brand"),
        subcategory=get("subcategory"),
        primary_colour=get("primary_colour"),
        dress_code=get("dress_code"),
        formality=_int_or_none(raw.get("formality")),
        warmth=_int_or_none(raw.get("warmth")),
        price_minor=_int_or_none(raw.get("price_minor")),
        currency=get("currency"),
        image_url=get("image_url"),
        in_stock=str(raw.get("in_stock") or "true").strip().lower() not in {"false", "0", "no"},
    )
