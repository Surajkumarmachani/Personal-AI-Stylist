"""Connect Gmail: the consent flow shares the calendar callback, the sync reads
only store emails into the same intake, and disconnecting never breaks the
calendar. Google is faked; Postgres, RLS and the real app are not."""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
import sqlalchemy as sa

from stylist_api.routers.calendar import STATE_AUDIENCE

pytestmark = pytest.mark.asyncio

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


@pytest.fixture
def google(monkeypatch):
    from stylist_api.settings import get_settings

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


async def _user_id(owner_engine, email: str) -> uuid.UUID:
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(sa.text("SELECT id FROM users WHERE email = :e"), {"e": email})
        ).scalar()


def _state(settings: Any, user_id: uuid.UUID, purpose: str) -> str:
    return jwt.encode(
        {
            "sub": str(user_id),
            "purpose": purpose,
            "aud": STATE_AUDIENCE,
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def _fake_grant(monkeypatch, scope: str) -> None:
    from stylist_clients import google_calendar as gcal

    async def exchange(code: str, **_: Any) -> Any:
        return gcal.TokenGrant(
            refresh_token="rt-gmail", access_token="at", scope=scope, expires_in=3600
        )

    async def email(token: str) -> str:
        return "shopper@gmail.com"

    monkeypatch.setattr(gcal, "exchange_code", exchange)
    monkeypatch.setattr(gcal, "account_email", email)


async def test_the_consent_url_asks_for_gmail_and_routes_back_through_the_calendar_callback(
    api, registered, google
) -> None:
    body = (await api.get("/gmail/connect", headers=registered.auth)).json()
    assert "gmail.readonly" in body["authorize_url"]
    assert "calendar%2Fcallback" in body["authorize_url"], "one registered redirect URI"


async def test_connecting_stores_a_gmail_link_beside_the_calendar(
    api, registered, google, owner_engine, monkeypatch
) -> None:
    _fake_grant(monkeypatch, f"{GMAIL_SCOPE} openid email")
    uid = await _user_id(owner_engine, registered.email)
    resp = await api.get(
        f"/calendar/callback?code=c&state={_state(google, uid, 'gmail')}", follow_redirects=False
    )
    assert resp.status_code == 303 and "gmail=connected" in resp.headers["location"]
    status = (await api.get("/gmail/status", headers=registered.auth)).json()
    assert status["connected"] is True and status["account_email"] == "shopper@gmail.com"
    # The calendar feature did not see this grant as a calendar.
    cal = (await api.get("/calendar/status", headers=registered.auth)).json()
    assert not cal.get("connected")


async def test_an_unticked_gmail_permission_is_refused_not_stored(
    api, registered, google, owner_engine, monkeypatch
) -> None:
    _fake_grant(monkeypatch, "openid email")
    uid = await _user_id(owner_engine, registered.email)
    resp = await api.get(
        f"/calendar/callback?code=c&state={_state(google, uid, 'gmail')}", follow_redirects=False
    )
    assert "gmail=failed" in resp.headers["location"]
    assert "gmail_permission_not_granted" in resp.headers["location"]
    assert (await api.get("/gmail/status", headers=registered.auth)).json()["connected"] is False


async def _link(owner_engine, uid: uuid.UUID, provider: str, token: str) -> None:
    from stylist_db.session import tenant_session

    async with tenant_session(uid) as db:
        await db.execute(
            sa.text(
                "INSERT INTO calendar_link (id, user_id, provider, refresh_token, account_email) "
                "VALUES (:i, :u, :p, :t, 'shopper@gmail.com')"
            ),
            {"i": uuid.uuid4(), "u": uid, "p": provider, "t": token},
        )


async def test_disconnecting_gmail_does_not_revoke_a_live_calendar(
    api, registered, google, owner_engine, monkeypatch
) -> None:
    from stylist_clients import google_calendar as gcal

    revoked: list[str] = []

    async def revoke(token: str) -> bool:
        revoked.append(token)
        return True

    monkeypatch.setattr(gcal, "revoke", revoke)
    uid = await _user_id(owner_engine, registered.email)
    await _link(owner_engine, uid, "google", "rt-calendar")
    await _link(owner_engine, uid, "google_gmail", "rt-gmail")

    body = (await api.delete("/gmail", headers=registered.auth)).json()
    assert body["disconnected"] and body["note"], "the user is told why"
    assert revoked == [], "revoking would have ended the calendar grant too"
    assert (await api.get("/gmail/status", headers=registered.auth)).json()["connected"] is False


def _raw_order(message_id: str) -> str:
    raw = (
        "From: Myntra <updates@mailer.myntra.com>\r\n"
        "To: shopper@gmail.com\r\n"
        "Subject: Your order is confirmed\r\n"
        f"Message-ID: <{message_id}>\r\n"
        "Content-Type: text/html; charset=utf-8\r\n\r\n"
        "<p>Roadster chinos</p>\r\n"
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


async def test_the_sync_files_store_emails_and_marks_a_dead_grant_for_reconnect(
    api, registered, google, owner_engine, monkeypatch
) -> None:
    from stylist_clients import google_calendar as gcal
    from stylist_clients import google_gmail
    from stylist_worker import gmail_sync

    uid = await _user_id(owner_engine, registered.email)
    await _link(owner_engine, uid, "google_gmail", "rt-gmail")
    message_id = f"sync-{uuid.uuid4()}"
    queries: list[str] = []

    async def refresh(token: str, **_: Any) -> Any:
        return gcal.TokenGrant(refresh_token=None, access_token="at", scope="", expires_in=3600)

    async def list_ids(token: str, *, query: str, limit: int) -> list[str]:
        queries.append(query)
        return ["g1"]

    async def get_raw(token: str, gmail_id: str) -> bytes:
        return base64.urlsafe_b64decode(_raw_order(message_id) + "==")

    monkeypatch.setattr(gcal, "refresh_access_token", refresh)
    monkeypatch.setattr(google_gmail, "list_message_ids", list_ids)
    monkeypatch.setattr(google_gmail, "get_raw", get_raw)

    result = await gmail_sync.sync_gmail_orders({})
    assert result["synced"] >= 1, result
    assert "from:(" in queries[0] and "myntra.com" in queries[0], "only store senders are searched"

    async with owner_engine.begin() as conn:
        row = (
            await conn.execute(
                sa.text(
                    "SELECT status, store FROM purchase_email "
                    "WHERE message_id = :m AND user_id = :u"
                ),
                {"m": f"<{message_id}>", "u": uid},
            )
        ).one()
    assert tuple(row) == ("received", "Myntra")
    status = (await api.get("/gmail/status", headers=registered.auth)).json()
    assert status["last_synced_at"] is not None

    # Google stops honouring the grant: marked for reconnect, not retried forever.
    async def dead(token: str, **_: Any) -> Any:
        raise gcal.CalendarReauthRequired("invalid_grant")

    monkeypatch.setattr(gcal, "refresh_access_token", dead)
    assert await gmail_sync.sync_one(uid) == {"reconnect_required": True}
    status = (await api.get("/gmail/status", headers=registered.auth)).json()
    assert status["connected"] is False and status["reconnect_required"] is True
