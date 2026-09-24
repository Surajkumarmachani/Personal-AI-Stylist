"""In the wash, or just worn: not in today's wardrobe.

Both rules have always held on the LIVE path, where `POOL_SQL` excludes
`needs_wash` and anything worn within `RECENTLY_WORN_DAYS`. These tests drive
the ENDPOINT instead, because suggestions are normally served from stored
outfits — materialised overnight, or persisted from an earlier live run — and
a stored outfit is a claim about the garments as they were when it was built.
Put the shirt in the basket and that claim is false until something re-checks
it.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text

OCCASION = "/suggestions?occasion=casual_outing&feels_like_c=26&limit=10"


async def _tenant(api, owner_engine) -> tuple[dict[str, str], dict[str, uuid.UUID]]:
    """A registered user with two of everything an outfit needs."""
    email = f"wash-{uuid.uuid4()}@example.com"
    resp = await api.post(
        "/auth/register", json={"email": email, "password": "a-long-enough-password"}
    )
    assert resp.status_code == 201, resp.text
    auth = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    kinds = {
        "top_a": ("upper_base", "t_shirt", "white"),
        "top_b": ("upper_base", "t_shirt", "blue_navy"),
        "jeans_a": ("lower", "jeans", "denim_indigo"),
        "jeans_b": ("lower", "chinos", "beige"),
        "shoes_a": ("feet", "sneakers", "white"),
        "shoes_b": ("feet", "sneakers", "black"),
    }
    ids = {name: uuid.uuid4() for name in kinds}
    async with owner_engine.begin() as conn:
        uid = (
            await conn.execute(text("SELECT id FROM users WHERE email = :e"), {"e": email})
        ).scalar_one()
        for name, (slot, sub, colour) in kinds.items():
            await conn.execute(
                text(
                    "INSERT INTO garments (id, user_id, original_key, state, slot, subcategory, "
                    "primary_colour, material, formality, warmth, dress_code) VALUES "
                    "(:g, :u, 'k', 'complete', CAST(:slot AS slot), "
                    "CAST(:sub AS subcategory), CAST(:colour AS colour), 'cotton', 2, 3, "
                    "'casual')"
                ),
                {"g": ids[name], "u": uid, "slot": slot, "sub": sub, "colour": colour},
            )
    return auth, ids


async def _suggested(api, auth) -> tuple[set[str], str]:
    resp = await api.get(OCCASION, headers=auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    seen = {g["id"] for o in body["outfits"] for g in o["garments"]}
    return seen, body.get("served_from") or ""


@pytest.mark.asyncio
async def test_a_garment_in_the_wash_is_not_suggested_from_stored_outfits(
    api, owner_engine
) -> None:
    auth, ids = await _tenant(api, owner_engine)
    first, _ = await _suggested(api, auth)
    assert str(ids["top_a"]) in first, "precondition: the shirt was being suggested"

    resp = await api.patch(
        f"/garments/{ids['top_a']}/laundry", json={"needs_wash": True}, headers=auth
    )
    assert resp.status_code == 200, resp.text

    after, served_from = await _suggested(api, auth)
    assert after, "the rest of the wardrobe can still dress this occasion"
    assert str(ids["top_a"]) not in after, f"shirt in the wash was suggested ({served_from})"

    # Out of the basket, back in the wardrobe.
    await api.patch(f"/garments/{ids['top_a']}/laundry", json={"needs_wash": False}, headers=auth)
    back, _ = await _suggested(api, auth)
    assert str(ids["top_a"]) in back


@pytest.mark.asyncio
async def test_a_garment_worn_today_is_not_suggested_from_stored_outfits(
    api, owner_engine
) -> None:
    auth, ids = await _tenant(api, owner_engine)
    first, _ = await _suggested(api, auth)
    assert str(ids["jeans_a"]) in first, "precondition: the jeans were being suggested"

    resp = await api.post(f"/garments/{ids['jeans_a']}/wear", json={}, headers=auth)
    assert resp.status_code == 200, resp.text

    after, served_from = await _suggested(api, auth)
    assert after
    assert str(ids["jeans_a"]) not in after, f"jeans worn today were suggested ({served_from})"


@pytest.mark.asyncio
async def test_worn_recently_but_marked_clean_is_still_rested(api, owner_engine) -> None:
    """Wearing sets `needs_wash`; taking it out of the basket clears that — but
    it was still worn yesterday, and the recently-worn rule is separate."""
    auth, ids = await _tenant(api, owner_engine)
    await _suggested(api, auth)

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    await api.post(f"/garments/{ids['shoes_a']}/wear", json={"worn_on": yesterday}, headers=auth)
    await api.patch(f"/garments/{ids['shoes_a']}/laundry", json={"needs_wash": False}, headers=auth)

    after, served_from = await _suggested(api, auth)
    assert after
    assert str(ids["shoes_a"]) not in after, f"shoes worn yesterday were suggested ({served_from})"


@pytest.mark.asyncio
async def test_washing_a_favourite_does_not_shrink_the_list(api, owner_engine) -> None:
    """The shirt sits in half of the stored outfits. Reading exactly `limit`
    rows and then dropping those left the user a fraction of what they asked
    for; the others in the precompute are still wearable and should be shown."""
    from stylist_worker.precompute import precompute_for_tenant

    auth, ids = await _tenant(api, owner_engine)
    async with owner_engine.begin() as conn:
        uid = (
            await conn.execute(
                text("SELECT user_id FROM garments WHERE id = :g"), {"g": ids["top_a"]}
            )
        ).scalar_one()
    await precompute_for_tenant(uid, warm_rationales=False, warm_boards=False)

    url = "/suggestions?occasion=casual_outing&limit=3"
    body = (await api.get(url, headers=auth)).json()
    assert body["served_from"] == "materialised", "precondition: served from the precompute"

    await api.patch(f"/garments/{ids['top_a']}/laundry", json={"needs_wash": True}, headers=auth)
    body = (await api.get(url, headers=auth)).json()
    worn = {g["id"] for o in body["outfits"] for g in o["garments"]}
    assert str(ids["top_a"]) not in worn
    # 2 tops x 2 bottoms x 2 shoes; without top_a, 4 outfits remain wearable.
    assert len(body["outfits"]) == 3, (len(body["outfits"]), body["served_from"])


@pytest.mark.asyncio
async def test_an_occasion_without_a_precompute_is_refilled_from_the_wardrobe(
    api, owner_engine
) -> None:
    """Most occasions only have the `limit` rows the last live run stored, so
    there are no spare rows to over-fetch. Measured: tee in the wash + jeans
    worn turned four stored outfits into one while two were still wearable."""
    auth, ids = await _tenant(api, owner_engine)
    url = "/suggestions?occasion=casual_outing&feels_like_c=26&limit=4"
    await api.get(url, headers=auth)
    await api.patch(f"/garments/{ids['top_a']}/laundry", json={"needs_wash": True}, headers=auth)
    await api.post(f"/garments/{ids['jeans_a']}/wear", json={}, headers=auth)

    body = (await api.get(url, headers=auth)).json()
    worn = {g["id"] for o in body["outfits"] for g in o["garments"]}
    assert not worn & {str(ids["top_a"]), str(ids["jeans_a"])}
    # top_b x jeans_b x (shoes_a | shoes_b): exactly two wearable outfits.
    assert len(body["outfits"]) == 2, (len(body["outfits"]), body["served_from"])
