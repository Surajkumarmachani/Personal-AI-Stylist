"""Gmail API, read-only: find store order emails in a user's own inbox.

"Connect Gmail" (Phase 15), the one-click alternative to forwarding. It uses the
same Google OAuth client, token exchange and refresh as Calendar
(google_calendar.py), with its own scope.

`gmail.readonly` IS A RESTRICTED SCOPE. Google allows it for up to 100 test
users while the OAuth app is in Testing (whose refresh tokens expire after 7
days), and requires verification plus an annual third-party security
assessment before the public can use it. Forwarding needs none of that, which is
why both paths exist. The app never asks for more than it reads: the search
below names store senders only, and a message is fetched only if it matched.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from stylist_clients.google_calendar import CalendarReauthRequired, CalendarUnavailable

SCOPES = ("https://www.googleapis.com/auth/gmail.readonly", "openid", "email")
MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class GmailReauthRequired(CalendarReauthRequired):
    """The grant is gone (revoked, expired, or the scope was withdrawn)."""


class GmailUnavailable(CalendarUnavailable):
    """Google is having a moment; try again next run."""


def _check(resp: httpx.Response) -> dict[str, Any]:
    if resp.status_code in (401, 403):
        raise GmailReauthRequired(f"gmail returned {resp.status_code}")
    if resp.status_code >= 400:
        raise GmailUnavailable(f"gmail returned {resp.status_code}")
    body: dict[str, Any] = resp.json()
    return body


async def list_message_ids(access_token: str, *, query: str, limit: int = 20) -> list[str]:
    """Newest-first ids of messages matching a Gmail search query."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(
                MESSAGES_URL,
                params={"q": query, "maxResults": limit},
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as exc:
        raise GmailUnavailable(f"gmail list failed: {type(exc).__name__}") from exc
    return [m["id"] for m in _check(resp).get("messages", [])]


async def get_raw(access_token: str, message_id: str) -> bytes:
    """The full RFC 822 message, the same bytes a mail server would deliver."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(
                f"{MESSAGES_URL}/{message_id}",
                params={"format": "raw"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as exc:
        raise GmailUnavailable(f"gmail get failed: {type(exc).__name__}") from exc
    raw = str(_check(resp).get("raw", ""))
    # base64url without padding.
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def order_query(store_domains: list[str], *, after_epoch: int) -> str:
    """Store senders, order-ish subjects, newer than the last sync.

    `-category:promotions` because stores send far more sale emails than order
    emails, and every one of those would otherwise cost a fetch and a model call
    to be told it is not an order.
    """
    senders = " OR ".join(store_domains)
    return (
        f"from:({senders}) "
        "subject:(order OR shipped OR delivered OR cancelled OR returned OR refund) "
        f"-category:promotions after:{after_epoch}"
    )
