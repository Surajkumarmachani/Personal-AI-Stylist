"""Read a forwarded order email and say which garments it bought.

THE FLOW
--------
The user forwards order confirmations (a one-time Gmail filter) to their private
address, orders-<token>@<INBOUND_EMAIL_DOMAIN>. The inbound webhook stores the
email; the worker calls `build_messages`, sends it to the gateway, and turns the
answer into garments with `parse_extraction` and `garment_fields`.

WHY AN LLM, AND WHY IT IS FENCED IN
-----------------------------------
Every store's email is different, and each redesigns its template without
notice, so per-store HTML scrapers break silently. A model reads all of them.
It is also the only way to get taxonomy fields from a product title like
"Roadster Men Navy Slim Fit Chinos".

But the answer lands in someone's wardrobe, so nothing it says is trusted as-is:
every enum is checked against the taxonomy, the image must be one that was
actually in the email (a model must not be able to point the fetcher at an
arbitrary URL), and a non-clothing line (a phone case in the same Amazon order)
is dropped.

WHY NOT THE PHOTO PIPELINE
--------------------------
Same reason as a catalogue purchase (see own.py): the store states what the item
is, and a store photo often shows a MODEL wearing it, which segmentation would
split into the shirt that was bought AND the jeans the model happened to wear.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

# Sender domain (or a parent of it) -> store name. The allow-list is what stops
# a newsletter or a stranger's email from becoming garments.
KNOWN_STORES: dict[str, str] = {
    "myntra.com": "Myntra",
    "ajio.com": "AJIO",
    "amazon.in": "Amazon",
    "amazon.com": "Amazon",
    "flipkart.com": "Flipkart",
    "nykaa.com": "Nykaa",
    "nykaafashion.com": "Nykaa Fashion",
    "tatacliq.com": "Tata CLiQ",
    "meesho.com": "Meesho",
    "hm.com": "H&M",
    "zara.com": "Zara",
    "uniqlo.com": "Uniqlo",
    "westside.com": "Westside",
    "shoppersstop.com": "Shoppers Stop",
    "lifestylestores.com": "Lifestyle",
    "maxfashion.in": "Max Fashion",
    "snitch.co.in": "Snitch",
    "bewakoof.com": "Bewakoof",
    "thesouledstore.com": "The Souled Store",
    "fabindia.com": "Fabindia",
    "libas.in": "Libas",
    "biba.in": "Biba",
    "manyavar.com": "Manyavar",
    "puma.com": "Puma",
    "nike.com": "Nike",
    "adidas.co.in": "Adidas",
}

GMAIL_FORWARDING_SENDER = "forwarding-noreply@google.com"

# What the model may say an email is. Only the first three add garments; the
# last two retire garments this feature added for the same order.
PURCHASE_KINDS = ("order_confirmed", "shipped", "delivered")
REVERSAL_KINDS = ("cancelled", "returned")
KINDS = (*PURCHASE_KINDS, *REVERSAL_KINDS, "other")

MAX_TEXT_CHARS = 20_000
MAX_IMAGES = 30

_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_SKIP_IMAGE = re.compile(
    r"logo|icon|pixel|spacer|tracking|beacon|facebook|twitter|instagram|youtube|"
    r"linkedin|pinterest|whatsapp|app-?store|play-?store|google-?play|badge|banner|"
    r"rating|star|social|footer|header|divider|arrow|bullet",
    re.IGNORECASE,
)


def addresses(value: str | None) -> list[str]:
    """Every email address in a header value, lower-cased."""
    return [a.lower() for a in _ADDRESS.findall(value or "")]


def inbox_token(recipients: list[str], domain: str) -> str | None:
    """The token from the first orders-<token>@<domain> recipient, if any."""
    if not domain:
        return None
    pattern = re.compile(rf"^orders-([a-z0-9]{{12,32}})@{re.escape(domain.lower())}$")
    for address in recipients:
        match = pattern.match(address.lower())
        if match:
            return match.group(1)
    return None


def store_for(sender: str) -> str | None:
    """The store behind an address, matching the domain or any parent of it
    (mailer.myntra.com is Myntra; myntra.com.evil.io is not)."""
    domain = sender.rsplit("@", 1)[-1].lower() if "@" in sender else ""
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        store = KNOWN_STORES.get(".".join(parts[i:]))
        if store:
            return store
    return None


@dataclass(frozen=True, slots=True)
class GmailConfirmation:
    code: str | None
    link: str | None


def gmail_confirmation(sender: str, text: str) -> GmailConfirmation | None:
    """Gmail's "confirm forwarding" email, which the user must act on.

    Adding a forwarding address in Gmail sends a code to THAT address, which is
    ours. Without surfacing it the user can never finish setup.
    """
    if sender.lower() != GMAIL_FORWARDING_SENDER:
        return None
    code = re.search(r"Confirmation code:\s*(\d{6,12})", text)
    link = re.search(r"https://mail(?:-settings)?\.google\.com/mail/[^\s\"'<>]+", text)
    return GmailConfirmation(
        code=code.group(1) if code else None, link=link.group(0) if link else None
    )


class _Reader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.images: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "head"}:
            self._skip += 1
        if tag in {"br", "p", "div", "tr", "li", "td", "h1", "h2", "h3"}:
            self.text.append("\n")
        if tag == "img":
            a = {k: (v or "") for k, v in attrs}
            src = a.get("src", "")
            if not src.startswith("https://") or _SKIP_IMAGE.search(src + " " + a.get("alt", "")):
                return
            try:
                if int(a.get("width") or 999) <= 60 or int(a.get("height") or 999) <= 60:
                    return
            except ValueError:
                pass
            if src not in self.images:
                self.images.append(src)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.text.append(data)


def read_email(html: str | None, text: str | None) -> tuple[str, list[str]]:
    """(plain text, candidate product-image URLs) for an email."""
    reader = _Reader()
    if html:
        reader.feed(html)
    body = "".join(reader.text) if html else (text or "")
    body = re.sub(r"[ \t\r\f\v]+", " ", body)
    body = re.sub(r"\n\s*\n+", "\n", body).strip()
    return body[:MAX_TEXT_CHARS], reader.images[:MAX_IMAGES]


def build_messages(
    *, store: str, subject: str, text: str, images: list[str], taxonomy: Any
) -> list[dict[str, Any]]:
    """The extraction prompt. Enums are spelled out so the answer can be validated."""
    subs = {slot: list(v) for slot, v in taxonomy.subcategories_by_slot.items()}
    schema = {
        "kind": f"one of {list(KINDS)}",
        "order_ref": "the order number, or null",
        "items": [
            {
                "title": "product name as written",
                "brand": "brand or null",
                "is_clothing": "true for clothes, shoes, bags and fashion accessories",
                "image": "the NUMBER of this item's photo from the image list, or null",
                "price": "amount paid for this line as a number, or null",
                "currency": "ISO code, e.g. INR",
                "size": "size or null",
                "slot": f"one of {list(subs)}",
                "subcategory": "one of the subcategories listed for that slot",
                "primary_colour": f"one of {list(taxonomy.colours)}",
                "dress_code": f"one of {list(taxonomy.dress_codes)}",
                "formality": "1 (loungewear) to 5 (black tie)",
                "warmth": "1 (very light) to 5 (heavy coat)",
            }
        ],
    }
    image_list = "\n".join(f"{i}: {url}" for i, url in enumerate(images)) or "(none)"
    return [
        {
            "role": "system",
            "content": (
                "You read online-shopping emails and extract what was bought. "
                "Reply with ONE JSON object and nothing else, in this shape:\n"
                f"{json.dumps(schema)}\n"
                f"Subcategories by slot: {json.dumps(subs)}\n"
                "Rules: list every line item; never invent an item, an image number "
                "or an order number that is not in the email; use null when unsure. "
                "A promotional or newsletter email is kind 'other' with no items."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Store: {store}\nSubject: {subject}\n\nImages:\n{image_list}\n\n"
                f"Email text:\n{text}"
            ),
        },
    ]


@dataclass(frozen=True, slots=True)
class OrderItem:
    title: str
    brand: str | None
    image_url: str | None
    price_minor: int | None
    currency: str | None
    size: str | None
    slot: str
    subcategory: str
    primary_colour: str | None
    dress_code: str | None
    formality: int | None
    warmth: int | None


@dataclass(frozen=True, slots=True)
class Extraction:
    kind: str
    order_ref: str | None
    items: tuple[OrderItem, ...]
    dropped: tuple[str, ...]  # titles skipped, with why, for the detail log


def _scale(value: Any) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 5 else None


def _clean(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s[:limit] or None


def parse_extraction(content: str, *, taxonomy: Any, images: list[str]) -> Extraction:
    """Validate the model's answer. Raises ValueError when it is not usable JSON."""
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in the model's reply")
    data = json.loads(match.group(0))

    kind = data.get("kind") if data.get("kind") in KINDS else "other"
    order_ref = _clean(data.get("order_ref"), 64)
    items: list[OrderItem] = []
    dropped: list[str] = []
    for raw in data.get("items") or []:
        if not isinstance(raw, dict):
            continue
        title = _clean(raw.get("title"), 200) or "item"
        if raw.get("is_clothing") is False or str(raw.get("is_clothing")).lower() == "false":
            dropped.append(f"{title}: not clothing")
            continue
        slot = str(raw.get("slot") or "")
        subcategory = str(raw.get("subcategory") or "")
        if slot not in taxonomy.subcategories_by_slot or (
            subcategory not in taxonomy.subcategories_by_slot[slot]
        ):
            # A subcategory alone still pins the slot.
            try:
                slot = taxonomy.default_slot_for(subcategory)
            except (KeyError, TypeError):
                dropped.append(f"{title}: could not tell what kind of garment it is")
                continue
        image_url = None
        try:
            index = int(raw.get("image"))  # type: ignore[arg-type]  # None -> TypeError
            if 0 <= index < len(images):
                image_url = images[index]
        except (TypeError, ValueError):
            pass
        price_minor = None
        try:
            if raw.get("price") is not None:
                price_minor = round(float(str(raw["price"]).replace(",", "")) * 100)
        except ValueError:
            pass
        colour = raw.get("primary_colour")
        dress_code = raw.get("dress_code")
        items.append(
            OrderItem(
                title=title,
                brand=_clean(raw.get("brand"), 80),
                image_url=image_url,
                price_minor=price_minor if price_minor and price_minor > 0 else None,
                currency=(_clean(raw.get("currency"), 3) or "INR").upper(),
                size=_clean(raw.get("size"), 20),
                slot=slot,
                subcategory=subcategory,
                primary_colour=colour if colour in taxonomy.colours else None,
                dress_code=dress_code if dress_code in taxonomy.dress_codes else None,
                formality=_scale(raw.get("formality")),
                warmth=_scale(raw.get("warmth")),
            )
        )
    return Extraction(kind=kind, order_ref=order_ref, items=tuple(items), dropped=tuple(dropped))


def purchase_key(store: str, order_ref: str | None, item: OrderItem) -> str:
    """Same order, same line -> same key, across confirmed/shipped/delivered."""
    basis = "|".join(
        [store.lower(), (order_ref or "").lower(), item.title.lower(), (item.size or "").lower()]
    )
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


def garment_fields(
    item: OrderItem, *, store: str, order_ref: str | None, email_id: str
) -> dict[str, Any]:
    """The garment row for a bought item. Pure, like own.garment_from_product."""
    return {
        "slot": item.slot,
        "subcategory": item.subcategory,
        "primary_colour": item.primary_colour,
        "dress_code": item.dress_code,
        "formality": item.formality,
        "warmth": item.warmth,
        "brand": item.brand,
        "purchase_price_minor": item.price_minor,
        "purchase_currency": item.currency if item.price_minor else None,
        "attributes_raw": {
            "source": "order_email",
            "store": store,
            "title": item.title,
            "size": item.size,
            "order_ref": order_ref,
            "email_id": email_id,
            "purchase_key": purchase_key(store, order_ref, item),
            "confirmed_by": "order_email",
        },
        # Read by a model from an email, not confirmed by the user by looking.
        "field_confidence": {},
        "extractor_version": "order-email-v1",
        "needs_review": True,
    }
