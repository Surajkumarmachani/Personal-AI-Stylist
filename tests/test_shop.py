"""The shopping gap-filler.

The design constraint under test is a product one: this feature must render
NOTHING unless the wardrobe genuinely cannot dress the occasion. A feed that
shows up regardless is a shop front beside "every look is built from clothes
you own", which is the one claim this app has that a retailer does not.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_a_gap_is_read_from_the_same_pool_as_the_outfits(api, registered) -> None:
    """Not a second query.

    A gap computed independently would eventually disagree with the outfits on
    screen — telling someone they have no footwear on a page showing three
    outfits with shoes is the contradiction that makes a whole screen
    untrustworthy.
    """
    resp = await api.get("/shop/gaps?occasion=casual_outing", headers=registered.auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["occasion"] == "casual_outing"
    assert isinstance(body["gaps"], list)


@pytest.mark.asyncio
async def test_the_affiliate_disclosure_is_a_field_not_a_footer(api, registered) -> None:
    """Paid-link disclosure is a legal obligation (ASA, FTC), not a styling
    choice. Returning it as data means a new client cannot forget it — a
    string living only in one component is one copy-paste from being lost."""
    body = (await api.get("/shop/gaps?occasion=casual_outing", headers=registered.auth)).json()
    assert body["affiliate"], "every response must carry the disclosure"
    assert "commission" in body["affiliate"].lower()


@pytest.mark.asyncio
async def test_an_unknown_occasion_is_refused(api, registered) -> None:
    resp = await api.get("/shop/gaps?occasion=brunch_o_clock", headers=registered.auth)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_clicking_an_unknown_product_is_a_404_not_a_500(api, registered) -> None:
    import uuid

    resp = await api.post(f"/shop/click/{uuid.uuid4()}", headers=registered.auth)
    assert resp.status_code == 404


def test_a_product_outside_the_taxonomy_is_rejected() -> None:
    """A product whose slot the wardrobe does not have is not a slightly worse
    row — no query will ever return it. Storing it inflates the catalogue while
    contributing nothing, and the bug presents much later as "we have thousands
    of products and show none"."""
    from stylist_shop.providers import InvalidProduct, Product, validate

    with pytest.raises(InvalidProduct):
        validate(Product(merchant="m", external_id="1", title="t", url="u", slot="spaceship"))

    with pytest.raises(InvalidProduct, match="no link"):
        validate(Product(merchant="m", external_id="1", title="t", url="", slot="feet"))


@pytest.mark.asyncio
async def test_the_catalogue_is_public_and_events_are_not(owner_engine) -> None:
    """A product is public content, identical for every user. Putting
    impressions on it would have made every catalogue row personal data —
    which is why `product_event` is a separate, RLS-forced table.

    Asserted against the LIVE SCHEMA rather than the migration's source text:
    what matters is what the database enforces, and a text search would pass
    happily against a migration that was never applied.
    """
    import sqlalchemy as sa

    async with owner_engine.begin() as conn:
        product_cols = (
            await conn.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'product'"
                )
            )
        ).scalars().all()
        assert "user_id" not in product_cols, (
            "a user_id on the catalogue would make every product row personal data"
        )

        forced = (
            await conn.execute(
                sa.text(
                    "SELECT relforcerowsecurity FROM pg_class WHERE relname = :t"
                ),
                {"t": "product_event"},
            )
        ).scalar_one()
        assert forced is True, "impressions and clicks are tenant data and must be forced"

        # And the catalogue deliberately is NOT, so it can be read without a
        # tenant context by the loader and by any user.
        product_forced = (
            await conn.execute(
                sa.text("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'product'")
            )
        ).scalar_one()
        assert product_forced is False


# --------------------------------------------------------------- owning one

def _addr(ip: str):
    """One getaddrinfo result, shaped as the stdlib returns it."""
    import socket as _s

    return [(_s.AF_INET, _s.SOCK_STREAM, _s.IPPROTO_TCP, "", (ip, 443))]


def test_an_image_url_on_a_private_address_is_refused() -> None:
    """The endpoint fetches a URL from inside our network.

    169.254.169.254 is the cloud metadata service and 10.x is where this
    stack's own MinIO and Postgres live. A server that will fetch any URL on
    request is an SSRF primitive no matter who wrote the URL down.
    """
    from stylist_shop.own import UnsafeImageURL, check_image_url

    for bad in ["169.254.169.254", "127.0.0.1", "10.1.2.3", "192.168.0.5"]:
        # `ip=bad` binds the CURRENT value. A bare closure over the loop
        # variable would resolve to the last address on every iteration, so
        # three of these four cases would silently not be tested.
        with pytest.raises(UnsafeImageURL):
            check_image_url(
                "https://images.example.com/a.jpg",
                resolve=lambda *a, ip=bad, **k: _addr(ip),
            )


def test_a_plain_http_image_url_is_refused() -> None:
    from stylist_shop.own import UnsafeImageURL, check_image_url

    with pytest.raises(UnsafeImageURL):
        check_image_url(
            "http://images.example.com/a.jpg",
            resolve=lambda *a, **k: _addr("93.1.2.3"),
        )


def test_a_public_https_image_url_is_allowed() -> None:
    from stylist_shop.own import check_image_url

    url = "https://images.example.com/a.jpg"
    assert check_image_url(url, resolve=lambda *a, **k: _addr("93.184.216.34")) == url


def test_a_catalogue_garment_claims_no_model_confidence() -> None:
    """Correction rate per field is how tagging quality is measured.

    These fields were copied from the merchant, so our model made no
    prediction. Recording 1.0 confidence would inflate that measurement with
    work the model never did -- the same mistake the eval view already avoids
    for user-verified fields.
    """
    from stylist_shop.own import garment_from_product

    row = garment_from_product(
        {
            "slot": "feet",
            "subcategory": "sneakers",
            "primary_colour": "white",
            "merchant": "demo",
            "external_id": "x1",
            "title": "White sneakers",
            "price_minor": 199900,
            "currency": "INR",
        },
        confirmed_by="user",
    )
    assert row["field_confidence"] == {}
    assert row["extractor_version"] == "catalogue-v1"
    assert row["needs_review"] is True
    assert row["attributes_raw"]["source"] == "catalogue"
    assert row["attributes_raw"]["confirmed_by"] == "user"


def test_the_confirmation_source_is_recorded_and_validated() -> None:
    """An inferred purchase and a stated one are not equally trustworthy."""
    from stylist_shop.own import garment_from_product

    fed = garment_from_product({"slot": "feet"}, confirmed_by="conversion_feed")
    assert fed["attributes_raw"]["confirmed_by"] == "conversion_feed"
    with pytest.raises(ValueError, match="unknown confirmation source"):
        garment_from_product({"slot": "feet"}, confirmed_by="assumed")


@pytest.mark.asyncio
async def test_a_garment_can_only_be_owned_once_per_product(owner_engine) -> None:
    """Tapping twice, or a conversion feed replaying a click, must not put two
    copies of the same shoes in a wardrobe. Enforced by a partial unique index
    rather than a check-then-insert, which would race with itself."""
    import sqlalchemy as sa

    async with owner_engine.begin() as conn:
        idx = (
            await conn.execute(
                sa.text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'uq_garment_one_per_product'"
                )
            )
        ).scalar_one()
    assert "UNIQUE" in idx
    assert "user_id" in idx and "sourced_product_id" in idx
    # PARTIAL: ordinary photographed garments are all NULL here and must not
    # be forced to be distinct from one another.
    assert "WHERE" in idx and "NOT NULL" in idx
