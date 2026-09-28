"""Reading forwarded order emails: pure parsing, no database, no network."""

from __future__ import annotations

import json

from stylist_domain.taxonomy import load_taxonomy
from stylist_shop.order_email import (
    gmail_confirmation,
    inbox_token,
    parse_extraction,
    purchase_key,
    read_email,
    store_for,
)

TAXONOMY = load_taxonomy()


def test_a_store_is_its_domain_or_a_parent_of_it_never_a_lookalike() -> None:
    assert store_for("orders@myntra.com") == "Myntra"
    assert store_for("no-reply@mailer.myntra.com") == "Myntra"
    assert store_for("auto-confirm@amazon.in") == "Amazon"
    assert store_for("support@myntra.com.evil.io") is None
    assert store_for("friend@gmail.com") is None


def test_the_token_is_read_only_from_our_domain() -> None:
    domain = "in.example.com"
    assert inbox_token(["orders-abcdef123456@in.example.com"], domain) == "abcdef123456"
    assert inbox_token(["ORDERS-ABCDEF123456@IN.EXAMPLE.COM"], domain) == "abcdef123456"
    assert inbox_token(["orders-abcdef123456@other.com"], domain) is None
    assert inbox_token(["orders-short@in.example.com"], domain) is None
    assert inbox_token(["orders-abcdef123456@in.example.com"], "") is None


def test_gmails_forwarding_code_is_found_so_the_user_can_finish_setup() -> None:
    text = (
        "Confirmation code: 482913077\n"
        "https://mail-settings.google.com/mail/vf-%5BANGjdJ%5D-abc to confirm"
    )
    found = gmail_confirmation("forwarding-noreply@google.com", text)
    assert found is not None and found.code == "482913077"
    assert found.link and found.link.startswith("https://mail-settings.google.com/mail/")
    assert gmail_confirmation("orders@myntra.com", text) is None


def test_product_photos_are_kept_and_logos_pixels_and_http_are_not() -> None:
    html = """
      <html><head><style>.x{}</style></head><body>
      <img src="https://assets.myntra.com/logo.png" width="120">
      <img src="https://img.example.com/p/123.jpg" width="300" alt="Chinos">
      <img src="https://track.example.com/open.gif" width="1" height="1">
      <img src="http://img.example.com/p/insecure.jpg">
      <img src="https://img.example.com/p/123.jpg">
      <p>Roadster Men Beige Slim Fit Chinos</p><p>Rs. 1,299</p>
      </body></html>
    """
    text, images = read_email(html, None)
    assert images == ["https://img.example.com/p/123.jpg"]
    assert "Roadster Men Beige Slim Fit Chinos" in text
    assert ".x{}" not in text


def _answer(**overrides: object) -> str:
    item = {
        "title": "Roadster Men Beige Slim Fit Chinos",
        "brand": "Roadster",
        "is_clothing": True,
        "image": 0,
        "price": "1,299",
        "currency": "inr",
        "size": "32",
        "slot": "lower",
        "subcategory": "chinos",
        "primary_colour": "beige",
        "dress_code": "smart_casual",
        "formality": 3,
        "warmth": 2,
    }
    item.update(overrides)
    return json.dumps({"kind": "order_confirmed", "order_ref": "MYN-1", "items": [item]})


def test_a_valid_answer_becomes_an_item_with_the_emails_own_photo() -> None:
    images = ["https://img.example.com/p/123.jpg"]
    result = parse_extraction(_answer(), taxonomy=TAXONOMY, images=images)
    assert result.kind == "order_confirmed" and result.order_ref == "MYN-1"
    (item,) = result.items
    assert item.image_url == images[0]
    assert item.price_minor == 129900 and item.currency == "INR"
    assert (item.slot, item.subcategory, item.primary_colour) == ("lower", "chinos", "beige")


def test_the_model_cannot_point_the_fetcher_outside_the_email() -> None:
    result = parse_extraction(_answer(image=7), taxonomy=TAXONOMY, images=["https://a/1.jpg"])
    assert result.items[0].image_url is None


def test_invalid_enums_are_dropped_not_trusted() -> None:
    result = parse_extraction(
        _answer(primary_colour="sunset glow", dress_code="party", formality=9),
        taxonomy=TAXONOMY,
        images=[],
    )
    item = result.items[0]
    assert item.primary_colour is None and item.dress_code is None and item.formality is None


def test_a_subcategory_alone_pins_the_slot_and_nonsense_is_skipped() -> None:
    fixed = parse_extraction(_answer(slot="bottoms"), taxonomy=TAXONOMY, images=[])
    assert fixed.items[0].slot == "lower"
    nonsense = parse_extraction(
        _answer(slot="gadget", subcategory="phone_case"), taxonomy=TAXONOMY, images=[]
    )
    assert nonsense.items == () and "could not tell" in nonsense.dropped[0]


def test_non_clothing_lines_are_dropped() -> None:
    result = parse_extraction(_answer(is_clothing=False), taxonomy=TAXONOMY, images=[])
    assert result.items == () and "not clothing" in result.dropped[0]


def test_an_unknown_kind_is_other_and_prose_around_the_json_is_tolerated() -> None:
    content = 'Sure! {"kind": "promo", "items": []} Hope that helps.'
    assert parse_extraction(content, taxonomy=TAXONOMY, images=[]).kind == "other"


def test_the_same_line_in_a_later_email_has_the_same_key() -> None:
    a = parse_extraction(_answer(), taxonomy=TAXONOMY, images=[]).items[0]
    b = parse_extraction(_answer(price=None, image=None), taxonomy=TAXONOMY, images=[]).items[0]
    assert purchase_key("Myntra", "MYN-1", a) == purchase_key("Myntra", "MYN-1", b)
    assert purchase_key("Myntra", "MYN-1", a) != purchase_key("Myntra", "MYN-2", a)
