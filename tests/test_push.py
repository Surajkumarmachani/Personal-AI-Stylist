"""Phase 8 — device registration and the 07:00 local daily digest.

Two properties carry this feature, and both are about restraint rather than
delivery: it must never send twice, and it must send at 07:00 where the USER
is rather than 07:00 somewhere convenient for the server.

FCM is faked at the client boundary. Nothing here reaches Google, and no test
needs a service-account credential — which is the point of reading the
credential from a path that can simply be unset.
"""

from __future__ import annotations

import datetime as dt
import uuid
import zoneinfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from stylist_clients.fcm import SendResult
from stylist_db.session import tenant_session
from stylist_worker import notify

TOKEN_A = "fcm-token-aaaaaaaaaaaaaaaaaaaa"
TOKEN_B = "fcm-token-bbbbbbbbbbbbbbbbbbbb"


class FakeFCM:
    """Records sends so "exactly once" is asserted by COUNT."""

    def __init__(self, *, permanent: tuple[tuple[str, str], ...] = (), raises=None):
        self.sends: list[dict] = []
        self._permanent = permanent
        self._raises = raises

    async def send(self, tokens, *, title, body, data=None):
        self.sends.append({"tokens": list(tokens), "title": title, "body": body, "data": data})
        if self._raises:
            raise self._raises
        dead = {t for t, _ in self._permanent}
        return SendResult(
            delivered=tuple(t for t in tokens if t not in dead),
            permanent_failure=self._permanent,
            transient_failure=(),
        )


@pytest.fixture(autouse=True)
def _reset_client():
    notify.reset()
    yield
    notify.reset()


@pytest.fixture
async def tenant(api, registered, owner_engine):
    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with maker() as session:
        row = await session.execute(
            text("SELECT id FROM users WHERE email = :e"), {"e": registered.email}
        )
        user_id = row.scalar_one()

    class T:
        pass

    t = T()
    t.id = user_id
    t.auth = registered.auth
    t.client = api
    return t


async def _register(tenant, token: str, *, tz: str | None = "Asia/Kolkata"):
    body = {"token": token, "platform": "android"}
    if tz:
        body["timezone"] = tz
    return await tenant.client.post("/push/devices", json=body, headers=tenant.auth)


async def _seed_outfit(tenant) -> None:
    async with tenant_session(tenant.id) as db:
        gid = uuid.uuid4()
        await db.execute(
            text(
                "INSERT INTO garments (id, user_id, original_key, slot, subcategory, state) "
                "VALUES (:g, :u, 'k', 'upper_base', 'kurta', 'complete')"
            ),
            {"g": gid, "u": tenant.id},
        )
        await db.execute(
            text(
                "INSERT INTO outfits (id, user_id, garment_ids, garment_set_hash, occasion, "
                "warmth_target, formality_target, score, scoring_version) "
                "VALUES (:i, :u, CAST(:ids AS uuid[]), :h, 'casual_outing', 3, 3, 0.9, 1)"
            ),
            {"i": uuid.uuid4(), "u": tenant.id, "ids": [str(gid)], "h": "a" * 64},
        )


def at_hour(hour: int, tz: str = "Asia/Kolkata") -> dt.datetime:
    return dt.datetime.now(zoneinfo.ZoneInfo(tz)).replace(hour=hour, minute=0)


# ------------------------------------------------------- registration


async def test_registering_the_same_device_twice_is_one_row(tenant) -> None:
    """The app POSTs its FCM token on every launch, because the token can
    change at any time. If that were not idempotent the table would fill with
    duplicates of one device and the user would get a notification per launch
    they have ever made."""
    first = await _register(tenant, TOKEN_A)
    second = await _register(tenant, TOKEN_A)

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["device_id"] == second.json()["device_id"]

    listed = (await tenant.client.get("/push/devices", headers=tenant.auth)).json()
    assert len(listed["devices"]) == 1


async def test_the_token_is_never_returned_to_the_client(tenant) -> None:
    """It identifies a device, and there is nothing a client can do with its
    own token that it does not already know."""
    await _register(tenant, TOKEN_A)
    body = (await tenant.client.get("/push/devices", headers=tenant.auth)).json()
    assert TOKEN_A not in str(body)


async def test_re_registering_revives_a_disabled_device(tenant) -> None:
    """A reinstall makes a previously-UNREGISTERED token live again. Refusing
    to reuse it would leave the user permanently unnotifiable with no way to
    fix it from inside the app."""
    device_id = (await _register(tenant, TOKEN_A)).json()["device_id"]
    await tenant.client.delete(f"/push/devices/{device_id}", headers=tenant.auth)

    listed = (await tenant.client.get("/push/devices", headers=tenant.auth)).json()
    assert listed["devices"][0]["disabled_at"] is not None

    await _register(tenant, TOKEN_A)
    listed = (await tenant.client.get("/push/devices", headers=tenant.auth)).json()
    assert listed["devices"][0]["disabled_at"] is None


async def test_turning_push_off_everywhere_is_one_action(tenant) -> None:
    """A user who wants notifications to stop should not have to find every
    device they have ever signed in on."""
    await _register(tenant, TOKEN_A)
    await _register(tenant, TOKEN_B)
    resp = await tenant.client.delete("/push/devices", headers=tenant.auth)

    assert resp.json()["disabled"] == 2
    listed = (await tenant.client.get("/push/devices", headers=tenant.auth)).json()
    assert all(d["disabled_at"] for d in listed["devices"])


# ------------------------------------------------ 07:00 local, exactly once


async def test_the_digest_sends_at_07_local_and_not_otherwise(tenant, monkeypatch) -> None:
    """There is no single moment that is 07:00 — it happens 24+ times a day
    across zones. The job runs hourly and fires only where it is 07:00 now."""
    await _register(tenant, TOKEN_A)
    await _seed_outfit(tenant)
    fake = FakeFCM()
    monkeypatch.setattr(notify, "get_client", lambda: fake)

    at_nine = await notify.send_digest_for_tenant(tenant.id, now=at_hour(9))
    assert at_nine["sent"] is False and at_nine["reason"] == "not_local_07"
    assert fake.sends == [], "nothing sent outside the window"

    at_seven = await notify.send_digest_for_tenant(tenant.id, now=at_hour(7))
    assert at_seven["sent"] is True
    assert fake.sends[0]["tokens"] == [TOKEN_A]


async def test_it_never_sends_twice_in_one_local_day(tenant, monkeypatch) -> None:
    """THE PROPERTY THAT MATTERS MOST. The cron can run on several replicas and
    can be retried; only the unique index on (user_id, kind, sent_on) makes the
    second attempt a no-op. Nobody forgives an app that notifies twice."""
    await _register(tenant, TOKEN_A)
    await _seed_outfit(tenant)
    fake = FakeFCM()
    monkeypatch.setattr(notify, "get_client", lambda: fake)

    first = await notify.send_digest_for_tenant(tenant.id, now=at_hour(7))
    second = await notify.send_digest_for_tenant(tenant.id, now=at_hour(7))

    assert first["sent"] is True
    assert second["sent"] is False and second["reason"] == "already_sent_today"
    assert len(fake.sends) == 1, "exactly one send, asserted by count"


async def test_the_send_log_is_append_only(tenant) -> None:
    """`push_send` is what makes "did we send today" answerable. An UPDATE
    could rewrite that and let a duplicate through, so the privilege is
    revoked — and a narrower GRANT would not have done it, because 0001's
    ALTER DEFAULT PRIVILEGES already attached UPDATE to every future table."""
    from sqlalchemy.exc import ProgrammingError

    async with tenant_session(tenant.id) as db:
        with pytest.raises(ProgrammingError, match="permission denied"):
            await db.execute(text("UPDATE push_send SET kind = 'x'"))


async def test_a_dead_token_is_disabled_rather_than_retried_forever(tenant, monkeypatch) -> None:
    """FCM says UNREGISTERED when the app was uninstalled. Retrying that token
    costs quota every morning for a device that no longer exists."""
    await _register(tenant, TOKEN_A)
    await _seed_outfit(tenant)
    fake = FakeFCM(permanent=((TOKEN_A, "UNREGISTERED"),))
    monkeypatch.setattr(notify, "get_client", lambda: fake)

    await notify.send_digest_for_tenant(tenant.id, now=at_hour(7))

    listed = (await tenant.client.get("/push/devices", headers=tenant.auth)).json()
    device = listed["devices"][0]
    assert device["disabled_at"] is not None
    assert device["disabled_reason"] == "UNREGISTERED"


async def test_push_being_unconfigured_is_not_an_error(tenant, monkeypatch) -> None:
    """The stack must come up and every other feature must work without a
    Firebase credential, exactly as it does without a provider key."""
    monkeypatch.setattr(notify, "get_client", lambda: None)
    result = await notify.send_digest_for_tenant(tenant.id, now=at_hour(7))
    assert result == {"sent": False, "reason": "push_not_configured"}


async def test_a_user_with_no_devices_is_skipped_quietly(tenant, monkeypatch) -> None:
    monkeypatch.setattr(notify, "get_client", lambda: FakeFCM())
    result = await notify.send_digest_for_tenant(tenant.id, now=at_hour(7))
    assert result["reason"] == "no_devices"


async def test_an_unparseable_timezone_does_not_skip_the_user_forever(tenant) -> None:
    """A device sending a non-IANA zone string must fall back rather than be
    excluded from every run for the rest of time."""
    await _register(tenant, TOKEN_A, tz="Not/AZone")
    assert notify._local_now("Not/AZone").tzinfo is not None


# ------------------------------------------------------------ the payload


async def test_the_notification_carries_a_deep_link_and_no_ranking_detail(
    tenant, monkeypatch
) -> None:
    """A push is one line on a lock screen — an invitation to open the app, not
    a place to explain a scorer. The outfit hash rides in `data` so a tap opens
    THAT board instead of a home screen."""
    await _register(tenant, TOKEN_A)
    await _seed_outfit(tenant)
    fake = FakeFCM()
    monkeypatch.setattr(notify, "get_client", lambda: fake)

    await notify.send_digest_for_tenant(tenant.id, now=at_hour(7))
    sent = fake.sends[0]

    assert sent["data"]["garment_set_hash"] == "a" * 64
    assert sent["data"]["kind"] == "daily_digest"
    assert "score" not in sent["body"].lower()
    assert "kurta" in sent["body"]
