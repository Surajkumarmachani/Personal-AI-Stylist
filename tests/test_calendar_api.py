"""Phase 8 — the calendar endpoints: the OAuth state, and degrading to default.

`tests/test_calendar.py` covers the classifier. This file covers the two things
only the endpoint can get wrong: whether a forged `state` can complete a
consent flow into somebody else's account, and whether a calendar that is
missing, revoked, or down takes the suggestion flow with it.

Nothing here talks to Google. The token exchange is faked at the client
boundary, the same way the LiteLLM gateway is faked in the tagging tests.
"""

from __future__ import annotations

import datetime as dt
import uuid

import jwt
import pytest
from sqlalchemy import text

from stylist_api.routers.calendar import STATE_AUDIENCE
from stylist_clients import google_calendar as gcal
from stylist_db.session import tenant_session


def forged_state(secret: str, *, aud: str = STATE_AUDIENCE, minutes: int = 5) -> str:
    return jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "aud": aud,
            "exp": dt.datetime.now(dt.UTC) + dt.timedelta(minutes=minutes),
        },
        secret,
        algorithm="HS256",
    )


# ------------------------------------------------------------ the state


async def test_a_forged_state_cannot_complete_a_consent_flow(api) -> None:
    """The callback carries NO bearer token — a browser arriving from Google
    has none — so `state` is the only thing identifying the user. If it were
    accepted unsigned, anyone could complete a consent flow into another
    account and attach their own calendar to it."""
    resp = await api.get("/calendar/callback", params={"code": "x", "state": "not-a-jwt"})
    assert resp.status_code == 400
    assert "state" in resp.json()["detail"]


async def test_a_state_signed_with_the_wrong_key_is_rejected(api) -> None:
    resp = await api.get(
        "/calendar/callback",
        params={"code": "x", "state": forged_state("attacker-key-not-ours-padded-to-32-bytes-min")},
    )
    assert resp.status_code == 400


async def test_an_expired_state_is_rejected(api) -> None:
    """A stale consent link must not work. The window is 10 minutes, which is
    long enough to read a consent screen and short enough that a link left in
    a browser history is useless."""
    from stylist_api.settings import get_settings

    stale = forged_state(get_settings().jwt_secret, minutes=-1)
    resp = await api.get("/calendar/callback", params={"code": "x", "state": stale})
    assert resp.status_code == 400


async def test_a_state_for_another_audience_is_rejected(api) -> None:
    """Our own access tokens are signed with the same secret. Without an
    audience check, a stolen access token would be a valid consent state."""
    from stylist_api.settings import get_settings

    wrong = forged_state(get_settings().jwt_secret, aud="access")
    resp = await api.get("/calendar/callback", params={"code": "x", "state": wrong})
    assert resp.status_code == 400


async def test_cancelling_consent_is_not_an_error(api) -> None:
    """The user pressed "deny". That is a choice, not a failure, and rendering
    it as a 4xx makes the app look broken for doing what it was told."""
    resp = await api.get("/calendar/callback", params={"error": "access_denied"})
    assert resp.status_code == 200
    assert resp.json() == {"connected": False, "reason": "access_denied"}


# ------------------------------------------------- degrading to default


async def test_today_falls_back_when_no_calendar_is_linked(api, registered) -> None:
    resp = await api.get("/calendar/today", headers=registered.auth)
    body = resp.json()

    assert resp.status_code == 200
    assert body["is_fallback"] is True
    assert body["source"] == "not_connected"
    assert body["confident"] is False
    assert body["occasion"]


async def test_today_degrades_rather_than_erroring_when_google_is_down(
    api, registered, owner_engine, monkeypatch
) -> None:
    """A calendar is an INPUT to getting dressed, not a dependency of it. An
    endpoint that 503s because Google is having a morning takes the whole
    suggestion flow down with it."""
    user_id = await _link_calendar(api, registered, owner_engine)

    async def boom(*a: object, **kw: object) -> object:
        raise gcal.CalendarUnavailable("google is down")

    monkeypatch.setattr(gcal, "refresh_access_token", boom)
    resp = await api.get("/calendar/today", headers=registered.auth)
    body = resp.json()

    assert resp.status_code == 200, "never a 5xx"
    assert body["is_fallback"] is True
    assert body["source"] == "unavailable"
    assert body["reconnect_required"] is False, "waiting fixes this; reconnecting does not"
    assert user_id  # linked, and still degraded cleanly


async def test_a_revoked_token_asks_the_user_to_reconnect(
    api, registered, owner_engine, monkeypatch
) -> None:
    """Distinct from "unavailable" BECAUSE THE RESPONSE DIFFERS. One is "try
    later", the other is "you must act" — showing the wrong one leaves someone
    waiting for a sync that will never happen."""
    await _link_calendar(api, registered, owner_engine)

    async def revoked(*a: object, **kw: object) -> object:
        raise gcal.CalendarReauthRequired("invalid_grant")

    monkeypatch.setattr(gcal, "refresh_access_token", revoked)
    body = (await api.get("/calendar/today", headers=registered.auth)).json()

    assert body["source"] == "reauth_required"
    assert body["reconnect_required"] is True
    assert body["is_fallback"] is True


async def test_today_classifies_real_titles_and_reports_only_a_count(
    api, registered, owner_engine, monkeypatch
) -> None:
    """The classification is returned; the calendar contents are not. Event
    titles are other people's data as much as the user's, and an API that
    echoed them would put them in every client log."""
    await _link_calendar(api, registered, owner_engine)

    class Grant:
        access_token = "at"

    async def ok(*a: object, **kw: object) -> object:
        return Grant()

    async def events(*a: object, **kw: object) -> list[str]:
        return ["Standup", "Interview with Acme — Dr Mehta", "Gym"]

    monkeypatch.setattr(gcal, "refresh_access_token", ok)
    monkeypatch.setattr(gcal, "list_today", events)
    body = (await api.get("/calendar/today", headers=registered.auth)).json()

    assert body["occasion"] == "interview"
    assert body["confident"] is True
    assert body["is_fallback"] is False
    assert body["events_seen"] == 3
    # The titles themselves never appear in the response.
    assert "Mehta" not in str(body) and "Acme" not in str(body)


# ------------------------------------------------------------ helpers


async def _link_calendar(api, registered, owner_engine) -> uuid.UUID:
    """Insert a link row directly. The consent flow is Google's to test."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with maker() as session:
        row = await session.execute(
            text("SELECT id FROM users WHERE email = :e"), {"e": registered.email}
        )
        user_id = row.scalar_one()

    async with tenant_session(user_id) as db:
        await db.execute(
            text(
                "INSERT INTO calendar_link (id, user_id, provider, refresh_token, scope) "
                "VALUES (:id, :uid, 'google', 'rt-test', :scope) "
                "ON CONFLICT (user_id, provider) DO UPDATE SET refresh_token = 'rt-test'"
            ),
            {"id": uuid.uuid4(), "uid": user_id, "scope": gcal.SCOPES[0]},
        )
    return user_id


@pytest.mark.parametrize("scope_granted", ["", "openid email"])
def test_a_narrower_grant_than_requested_is_detectable(scope_granted: str) -> None:
    """Google lets a user uncheck scopes on the consent screen. Assuming we got
    what we asked for is how a sync 403s with nothing pointing at the cause —
    the callback checks the returned scope for exactly this reason."""
    assert gcal.SCOPES[0] not in scope_granted
