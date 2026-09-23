"""Turn a catalogue product into a garment the user owns (Phase 13).

WHY THIS SKIPS THE WHOLE INGEST PIPELINE
-----------------------------------------
A photographed garment goes through segment -> matte -> tag because we start
with a picture and have to work out what it is. A catalogue product arrives
with slot, subcategory, colour, dress code, formality and warmth already
stated by the merchant. Running a vision model over a packshot to re-derive
facts we were handed would be slower, cost money, and be WORSE: the model
guesses, the merchant knows.

So the fields are copied, and `field_confidence` is left empty rather than
filled with 1.0. A confidence of 1.0 would be a claim our model made a perfect
prediction. It made no prediction at all.

THE "DID THEY BUY IT" PROBLEM, STATED PLAINLY
----------------------------------------------
Nothing here can tell that a purchase happened. We see the click; the checkout
is on the merchant's site. This module is therefore driven by the user saying
"I bought this", and is written so that a future affiliate conversion feed can
call exactly the same function with `confirmed_by="conversion_feed"` instead
of `"user"` — the record of WHICH is kept, because an inferred purchase and a
stated one are not equally trustworthy and a later cleanup will need to tell
them apart.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

# Packshots are small. Anything larger is not a product photo, and streaming
# an unbounded body into memory from a URL in a CSV is how a loader becomes a
# denial-of-service vector.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

ALLOWED_IMAGE_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/avif"}
)


class UnsafeImageURL(Exception):  # noqa: N818
    """The image URL must not be fetched."""


def check_image_url(url: str, *, resolve: Any = socket.getaddrinfo) -> str:
    """Reject anything we should not make a server-side request to.

    The catalogue is loaded from a CSV, so these URLs are operator-supplied
    rather than user-supplied — but this endpoint fetches them from INSIDE the
    network, where `http://169.254.169.254/` is the cloud metadata service and
    `http://minio:9000/` is our own object store. A server that will fetch an
    arbitrary URL on request is an SSRF primitive regardless of who wrote the
    URL down, and the catalogue is exactly the kind of file that later gets a
    "just let the supplier upload it" feature.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UnsafeImageURL("image_url must be https")
    if not parsed.hostname:
        raise UnsafeImageURL("image_url has no host")

    try:
        infos = resolve(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise UnsafeImageURL(f"cannot resolve {parsed.hostname}") from exc

    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        # Covers loopback, link-local (169.254.169.254), RFC1918 and the
        # container network the rest of this stack lives on.
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
        ):
            raise UnsafeImageURL(f"{parsed.hostname} resolves to non-public {addr}")
    return url


def garment_from_product(product: dict[str, Any], *, confirmed_by: str) -> dict[str, Any]:
    """The garment row for a bought product. Pure: no IO, no clock, no uuid.

    The caller supplies ids and keys, so this stays trivially testable and the
    mapping from merchant field to garment field is readable in one screen.
    """
    if confirmed_by not in {"user", "conversion_feed"}:
        raise ValueError(f"unknown confirmation source: {confirmed_by}")

    return {
        "slot": product.get("slot"),
        "subcategory": product.get("subcategory"),
        "primary_colour": product.get("primary_colour"),
        "dress_code": product.get("dress_code"),
        "formality": product.get("formality"),
        "warmth": product.get("warmth"),
        "brand": product.get("brand"),
        "purchase_price_minor": product.get("price_minor"),
        "purchase_currency": product.get("currency"),
        # Provenance, kept in the row itself so a later audit does not have to
        # join back to a product that may since have left the catalogue.
        "attributes_raw": {
            "source": "catalogue",
            "merchant": product.get("merchant"),
            "external_id": product.get("external_id"),
            "title": product.get("title"),
            "confirmed_by": confirmed_by,
        },
        # EMPTY on purpose. See the module docstring: our model made no
        # prediction here, so there is no confidence to report.
        "field_confidence": {},
        "extractor_version": "catalogue-v1",
        # The merchant stated these; the user has not confirmed them by
        # looking. Flagging for review is how the wardrobe editor surfaces it.
        "needs_review": True,
    }
