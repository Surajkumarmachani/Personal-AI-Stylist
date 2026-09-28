"""Forwarded order emails, end to end: webhook -> stored email -> worker ->
garment in the wardrobe, once, and gone again on a return.

The gateway and the photo fetch are faked; Postgres (with RLS), the real app
and the real worker function are not.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

DOMAIN = "in.example.com"
SECRET = "inbound-s3cret"
PHOTO = "https://img.example.com/p/chinos.jpg"


@pytest.fixture
def inbound(monkeypatch):
    from stylist_api.settings import get_settings

    monkeypatch.setenv("INBOUND_EMAIL_DOMAIN", DOMAIN)
    monkeypatch.setenv("INBOUND_EMAIL_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _email(to: str, *, sender: str, subject: str, html: str, message_id: str) -> dict[str, str]:
    return {
        "to": to,
        "from": sender,
        "subject": subject,
        "html": html,
        "text": "",
        "headers": f"Message-ID: <{message_id}>\nSubject: {subject}\n",
        "envelope": json.dumps({"to": [to], "from": sender}),
    }


async def _post(api, fields: dict[str, str], key: str = SECRET):
    return await api.post(f"/inbound/email?key={key}", data=fields)


async def _address(api, auth) -> str:
    body = (await api.get("/me/purchase-inbox", headers=auth)).json()
    assert body["enabled"] is True, body
    return body["address"]


ORDER_HTML = f"""<html><body>
  <img src="{PHOTO}" width="300">
  <p>Your order MYN-7 is confirmed</p><p>Roadster Men Beige Slim Fit Chinos, size 32</p>
  <p>Rs. 1,299</p></body></html>"""


def _model_answer(kind: str) -> str:
    return json.dumps(
        {
            "kind": kind,
            "order_ref": "MYN-7",
            "items": [
                {
                    "title": "Roadster Men Beige Slim Fit Chinos",
                    "brand": "Roadster",
                    "is_clothing": True,
                    "image": 0,
                    "price": 1299,
                    "currency": "INR",
                    "size": "32",
                    "slot": "lower",
                    "subcategory": "chinos",
                    "primary_colour": "beige",
                    "dress_code": "smart_casual",
                    "formality": 3,
                    "warmth": 2,
                },
                {"title": "Phone case", "is_clothing": False},
            ],
        }
    )


@pytest.mark.asyncio
async def test_the_webhook_is_closed_without_a_configured_secret(api, monkeypatch) -> None:
    from stylist_api.settings import get_settings

    monkeypatch.setenv("INBOUND_EMAIL_SECRET", "")
    get_settings.cache_clear()
    try:
        assert (await api.post("/inbound/email?key=", data={"to": "x"})).status_code == 404
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_a_wrong_key_is_refused(api, inbound) -> None:
    assert (await _post(api, {"to": "x"}, key="nope")).status_code == 404


@pytest.mark.asyncio
async def test_the_address_is_private_and_rotates(api, registered, inbound) -> None:
    first = await _address(api, registered.auth)
    assert first.startswith("orders-") and first.endswith(f"@{DOMAIN}")
    assert await _address(api, registered.auth) == first
    rotated = (await api.post("/me/purchase-inbox/rotate", headers=registered.auth)).json()
    assert rotated["address"] != first
    # The old address now belongs to nobody.
    stale = _email(first, sender="orders@myntra.com", subject="x", html="x", message_id="m-old")
    assert (await _post(api, stale)).json() == {"accepted": False, "reason": "unknown inbox"}


@pytest.mark.asyncio
async def test_gmails_confirmation_code_is_shown_in_the_app(api, registered, inbound) -> None:
    to = await _address(api, registered.auth)
    fields = _email(
        to,
        sender="Gmail Team <forwarding-noreply@google.com>",
        subject="Gmail Forwarding Confirmation",
        html="<p>Confirmation code: 482913077</p>",
        message_id=f"gmail-{uuid.uuid4()}",
    )
    assert (await _post(api, fields)).json()["status"] == "gmail_confirmation"
    body = (await api.get("/me/purchase-inbox", headers=registered.auth)).json()
    assert body["gmail_confirmation"]["code"] == "482913077"


@pytest.mark.asyncio
async def test_a_stranger_or_newsletter_is_recorded_as_ignored(api, registered, inbound) -> None:
    to = await _address(api, registered.auth)
    fields = _email(
        to, sender="deals@spam.example", subject="50% off", html="<p>hi</p>", message_id="spam-1"
    )
    assert (await _post(api, fields)).json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_an_order_email_becomes_one_garment_and_a_return_retires_it(
    api, registered, inbound, owner_engine, monkeypatch
) -> None:
    from stylist_worker import purchases

    to = await _address(api, registered.auth)
    async with owner_engine.begin() as conn:
        user_id = (
            await conn.execute(
                sa.text("SELECT id FROM users WHERE email = :e"), {"e": registered.email}
            )
        ).scalar()
        # Signup could not reach a gateway in tests, so give the tenant a key.
        await conn.execute(
            sa.text("UPDATE user_profile SET litellm_key = 'sk-test' WHERE user_id = :u"),
            {"u": user_id},
        )

    answers: list[str] = []

    class FakeGateway:
        async def chat(self, **kwargs: Any) -> Any:
            assert kwargs["api_key"] == "sk-test", "must use the tenant's key"
            return SimpleNamespace(content=answers.pop(0))

    stored: dict[str, bytes] = {}

    class FakeStore:
        def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
            stored[key] = data

    async def fake_fetch(url: str) -> tuple[bytes, str]:
        assert url == PHOTO, "only a photo from the email may be fetched"
        return b"jpeg-bytes", "image/jpeg"

    monkeypatch.setattr(purchases, "get_litellm_client", lambda: FakeGateway())
    monkeypatch.setattr(purchases, "get_object_store", lambda: FakeStore())
    monkeypatch.setattr(purchases, "fetch_packshot", fake_fetch)

    async def deliver(message_id: str, kind: str) -> dict[str, Any]:
        resp = await _post(
            api,
            _email(
                to,
                sender="Myntra <updates@mailer.myntra.com>",
                subject=f"Order {kind}",
                html=ORDER_HTML,
                message_id=message_id,
            ),
        )
        assert resp.json()["status"] == "received", resp.json()
        async with owner_engine.begin() as conn:
            email_id = (
                await conn.execute(
                    sa.text("SELECT id FROM purchase_email WHERE message_id = :m"),
                    {"m": f"<{message_id}>"},
                )
            ).scalar()
        answers.append(_model_answer(kind))
        return await purchases.ingest_order_email(
            {}, user_id=str(user_id), aggregate_id=str(email_id), payload={}
        )

    async def chinos() -> list[Any]:
        async with owner_engine.begin() as conn:
            return list(
                (
                    await conn.execute(
                        sa.text(
                            "SELECT is_active, brand, purchase_price_minor, cutout_key, "
                            "subcategory::text, attributes_raw->>'store' FROM garments "
                            "WHERE user_id = :u AND attributes_raw->>'source' = 'order_email'"
                        ),
                        {"u": user_id},
                    )
                ).all()
            )

    tag = uuid.uuid4().hex
    assert await deliver(f"confirmed-{tag}", "order_confirmed") == {"added": 1, "retired": 0}
    (row,) = await chinos()
    assert row[0] is True and row[1] == "Roadster" and row[2] == 129900
    assert row[4] == "chinos" and row[5] == "Myntra"
    assert row[3] in stored, "the store's photo is the garment's picture"

    # The same order again, as the shipping email: still one pair of chinos.
    assert await deliver(f"shipped-{tag}", "shipped") == {"added": 0, "retired": 0}
    assert len(await chinos()) == 1

    # A provider retrying the SAME email is a duplicate, not a second row.
    replay = _email(
        to,
        sender="updates@mailer.myntra.com",
        subject="x",
        html=ORDER_HTML,
        message_id=f"shipped-{tag}",
    )
    assert (await _post(api, replay)).json() == {"accepted": True, "duplicate": True}

    # Returned: it leaves the wardrobe.
    assert await deliver(f"returned-{tag}", "returned") == {"added": 0, "retired": 1}
    (row,) = await chinos()
    assert row[0] is False

    # The raw email is not kept once processed.
    async with owner_engine.begin() as conn:
        bodies = (
            await conn.execute(
                sa.text(
                    "SELECT count(*) FROM purchase_email "
                    "WHERE user_id = :u AND (body_html IS NOT NULL OR body_text IS NOT NULL)"
                ),
                {"u": user_id},
            )
        ).scalar()
    assert bodies == 0


# ---------------------------------------------------------------- gmail inbox

GMAIL_INBOX = "your.stylist.orders@gmail.com"


@pytest.fixture
def gmail_inbox(monkeypatch):
    from stylist_api.settings import get_settings

    monkeypatch.setenv("INBOUND_EMAIL_DOMAIN", "")
    monkeypatch.setenv("INBOUND_GMAIL_ADDRESS", GMAIL_INBOX)
    monkeypatch.setenv("INBOUND_GMAIL_APP_PASSWORD", "app-password")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _auto_forwarded(plus_address: str, message_id: str) -> bytes:
    """What Gmail delivers after a filter forwards a Myntra email: the ORIGINAL
    headers (To: is still the user), with the +token only in Delivered-To."""
    return (
        f"Delivered-To: {plus_address}\r\n"
        "From: Myntra <updates@mailer.myntra.com>\r\n"
        "To: shopper@example.com\r\n"
        "Subject: Your order MYN-9 is confirmed\r\n"
        f"Message-ID: <{message_id}>\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/alternative; boundary="b"\r\n\r\n'
        "--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nRoadster chinos\r\n"
        "--b\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        f'<p>Roadster chinos</p><img src="{PHOTO}" width="300">\r\n'
        "--b--\r\n"
    ).encode()


def test_the_plus_token_is_read_from_delivered_to_not_to() -> None:
    from stylist_worker.inbox_poll import fields_from_message

    fields = fields_from_message(
        _auto_forwarded(f"{GMAIL_INBOX[:-10]}+abcdef123456@gmail.com", "m1")
    )
    assert "your.stylist.orders+abcdef123456@gmail.com" in fields["recipients"]
    assert "shopper@example.com" in fields["recipients"]
    assert fields["sender_header"] == "Myntra <updates@mailer.myntra.com>"
    assert PHOTO in (fields["html"] or "") and "Roadster chinos" in (fields["plain"] or "")
    assert fields["headers"] == "Message-ID: <m1>\n"


@pytest.mark.asyncio
async def test_the_gmail_poller_files_mail_and_marks_only_accepted_mail_seen(
    api, registered, gmail_inbox, owner_engine, monkeypatch
) -> None:
    from stylist_worker import inbox_poll

    body = (await api.get("/me/purchase-inbox", headers=registered.auth)).json()
    address = body["address"]
    assert address.startswith("your.stylist.orders+") and address.endswith("@gmail.com")

    message_id = f"poll-{uuid.uuid4()}"
    inbox = [(b"7", _auto_forwarded(address, message_id)), (b"8", b"not an email at all")]
    marked: list[bytes] = []
    monkeypatch.setattr(inbox_poll, "_fetch_unseen", lambda user, password: list(inbox))
    monkeypatch.setattr(inbox_poll, "_mark_seen", lambda user, password, uids: marked.extend(uids))

    result = await inbox_poll.poll_order_inbox({})
    assert result["read"] == 2 and result.get("received") == 1, result
    assert b"7" in marked

    async with owner_engine.begin() as conn:
        row = (
            await conn.execute(
                sa.text("SELECT status, store FROM purchase_email WHERE message_id = :m"),
                {"m": f"<{message_id}>"},
            )
        ).one()
    assert tuple(row) == ("received", "Myntra")

    # Polled again (say the mark-seen failed): a duplicate, not a second email.
    assert (await inbox_poll.poll_order_inbox({})).get("duplicate") == 1


@pytest.mark.asyncio
async def test_the_poller_does_nothing_until_configured(monkeypatch) -> None:
    from stylist_api.settings import get_settings
    from stylist_worker import inbox_poll

    monkeypatch.setenv("INBOUND_GMAIL_ADDRESS", "")
    get_settings.cache_clear()
    try:
        assert await inbox_poll.poll_order_inbox({}) == {"skipped": "no gmail inbox configured"}
    finally:
        get_settings.cache_clear()
