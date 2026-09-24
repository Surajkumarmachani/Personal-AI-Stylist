"""Partner servers: API keys, and acting for the partner's own users.

The property that matters most is the boundary: a key reaches the users ITS
partner created and nobody else — not another partner's user with the same
id, and not a person who signed up directly.
"""

from __future__ import annotations

import uuid

import pytest


async def _client(api, admin, *, rate: int = 120) -> tuple[str, str]:
    """A partner and one key for it. Returns (client_id, key)."""
    made = await api.post(
        "/admin/api-clients",
        json={"name": f"partner-{uuid.uuid4().hex[:6]}", "rate_limit_per_minute": rate},
        headers=admin.auth,
    )
    assert made.status_code == 201, made.text
    client_id = made.json()["id"]
    issued = await api.post(
        f"/admin/api-clients/{client_id}/keys", json={"label": "test"}, headers=admin.auth
    )
    assert issued.status_code == 201, issued.text
    return client_id, issued.json()["key"]


def _as(key: str, user: str | None = None) -> dict[str, str]:
    return {"X-API-Key": key, **({"X-User-Id": user} if user else {})}


@pytest.mark.asyncio
async def test_a_partner_creates_a_user_and_uses_the_ordinary_endpoints(api, admin) -> None:
    _, key = await _client(api, admin)

    first = await api.post(
        "/partner/users", json={"external_id": "cust_1", "dresses_as": "men"}, headers=_as(key)
    )
    assert first.status_code == 201 and first.json()["created"] is True
    again = await api.post("/partner/users", json={"external_id": "cust_1"}, headers=_as(key))
    assert again.status_code == 200 and again.json()["created"] is False

    # The same endpoints a person uses, with no partner-only copies.
    assert (await api.get("/garments", headers=_as(key, "cust_1"))).status_code == 200
    assert (await api.get("/me/dresses-as", headers=_as(key, "cust_1"))).json()[
        "dresses_as"
    ] == "men"
    chat = await api.post(
        "/chat", json={"message": "what should I wear to the office"}, headers=_as(key, "cust_1")
    )
    assert chat.status_code == 200, chat.text


@pytest.mark.asyncio
async def test_a_key_only_reaches_its_own_partners_users(api, admin, registered) -> None:
    _, key_a = await _client(api, admin)
    _, key_b = await _client(api, admin)
    for key in (key_a, key_b):
        await api.post("/partner/users", json={"external_id": "same_id"}, headers=_as(key))

    await api.put(
        "/me/dresses-as", json={"dresses_as": "women"}, headers=_as(key_a, "same_id")
    )
    # Partner B's "same_id" is a different person.
    b = (await api.get("/me/dresses-as", headers=_as(key_b, "same_id"))).json()
    assert b["dresses_as"] is None

    # A person who signed up directly cannot be named through a key at all.
    assert (await api.get("/garments", headers=_as(key_a, registered.email))).status_code == 404


@pytest.mark.asyncio
async def test_the_obvious_mistakes_get_clear_errors(api, admin) -> None:
    _, key = await _client(api, admin)
    assert (await api.get("/garments", headers=_as(key))).status_code == 400  # no X-User-Id
    assert (await api.get("/garments", headers=_as(key, "nobody"))).status_code == 404
    assert (await api.get("/garments", headers=_as("sty_notreal12345_x", "a"))).status_code == 401
    assert (await api.get("/garments", headers=_as(key + "tampered", "a"))).status_code == 401
    assert (await api.post("/partner/users", json={"external_id": "x"})).status_code == 401


@pytest.mark.asyncio
async def test_a_revoked_key_stops_working_at_once(api, admin) -> None:
    client_id, key = await _client(api, admin)
    await api.post("/partner/users", json={"external_id": "cust_r"}, headers=_as(key))
    assert (await api.get("/garments", headers=_as(key, "cust_r"))).status_code == 200

    listed = (await api.get("/admin/api-clients", headers=admin.auth)).json()["clients"]
    mine = next(c for c in listed if c["id"] == client_id)
    key_id = mine["keys"][0]["id"]
    assert (await api.delete(f"/admin/api-keys/{key_id}", headers=admin.auth)).status_code == 204
    assert (await api.get("/garments", headers=_as(key, "cust_r"))).status_code == 401


@pytest.mark.asyncio
async def test_a_listing_never_shows_a_secret(api, admin) -> None:
    _, key = await _client(api, admin)
    body = (await api.get("/admin/api-clients", headers=admin.auth)).text
    secret = key.split("_", 2)[2]
    assert secret not in body
    assert key.split("_", 2)[1] in body, "the prefix is shown so keys can be told apart"


@pytest.mark.asyncio
async def test_only_admins_manage_partners(api, registered) -> None:
    resp = await api.post("/admin/api-clients", json={"name": "x"}, headers=registered.auth)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_the_rate_limit_is_per_partner(api, admin) -> None:
    _, key = await _client(api, admin, rate=3)
    await api.post("/partner/users", json={"external_id": "cust_rl"}, headers=_as(key))
    codes = [
        (await api.get("/garments", headers=_as(key, "cust_rl"))).status_code for _ in range(4)
    ]
    # 1 create + 2 reads fit in 3; the next ones are refused, with Retry-After.
    assert codes[:2] == [200, 200] and 429 in codes, codes
    limited = await api.get("/garments", headers=_as(key, "cust_rl"))
    assert limited.status_code == 429 and "retry-after" in limited.headers


@pytest.mark.asyncio
async def test_a_partner_user_cannot_log_in_with_a_password(api, admin, owner_engine) -> None:
    """They have no password; the partner acts for them. The stored hash
    matches nothing, including the empty string and itself."""
    import sqlalchemy as sa

    _, key = await _client(api, admin)
    await api.post("/partner/users", json={"external_id": "cust_pw"}, headers=_as(key))
    async with owner_engine.begin() as conn:
        email, pw = (
            await conn.execute(
                sa.text(
                    "SELECT email, password_hash FROM users WHERE external_id = 'cust_pw' "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).one()
    assert email.endswith("@partner.invalid")
    for attempt in ("", pw, "a-long-enough-password"):
        resp = await api.post("/auth/login", json={"email": email, "password": attempt})
        assert resp.status_code in (401, 422), (attempt, resp.status_code)


@pytest.mark.asyncio
async def test_a_partner_can_erase_its_user(api, admin) -> None:
    """The documented deletion path works through a key: the erasure saga
    starts, and the user is gone to the partner from then on (410)."""
    _, key = await _client(api, admin)
    await api.post("/partner/users", json={"external_id": "cust_del"}, headers=_as(key))
    resp = await api.delete("/me?confirm=DELETE", headers=_as(key, "cust_del"))
    assert resp.status_code == 202, resp.text
    assert (await api.get("/garments", headers=_as(key, "cust_del"))).status_code == 410
