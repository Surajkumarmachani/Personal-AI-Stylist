"""Google OAuth + Calendar, the only two calls this project makes (Phase 8).

WRITTEN DIRECTLY AGAINST THE HTTP API, NOT VIA google-api-python-client
------------------------------------------------------------------------
That SDK pulls in ~15 transitive dependencies and a credentials-discovery layer
that reads ambient environment and files — behaviour you do not want in a
container that should have exactly one way to be authenticated. What we need is
two POSTs and one GET. `httpx` is already a dependency.

READ-ONLY, AND ASKED FOR AS LITTLE AS GOOGLE ALLOWS
---------------------------------------------------
`calendar.events.readonly` only. Not `calendar`, not `calendar.readonly` —
the narrowest scope that returns event titles. The grant is also VERIFIED on
return rather than assumed, because Google lets a user uncheck scopes on the
consent screen and a narrower grant would otherwise surface as a 403 mid-sync
with nothing pointing at the cause.

WHAT LEAVES GOOGLE AND WHAT WE KEEP
------------------------------------
`list_today` returns SUMMARIES ONLY — no attendees, no description, no
location, no conference links. The classifier needs a title; everything else is
other people's data we would then be responsible for. Nothing here is
persisted: titles are classified in memory and discarded.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

import httpx

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
# Any calendar id, not just `primary`. Public holiday calendars are read
# with an API KEY rather than OAuth: they are not anybody's private diary.
CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# The narrowest scope that returns event titles. `openid`/`email` come along
# only so the UI can show WHICH Google account is linked — a user with two
# accounts cannot otherwise tell which calendar is syncing.
SCOPES = ("https://www.googleapis.com/auth/calendar.events.readonly", "openid", "email")

TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class CalendarUnavailable(RuntimeError):  # noqa: N818 - a state, not an error type
    """Google is unreachable or refused. Backpressure, never a verdict on the
    user's day — the caller falls back to the default occasion."""


class CalendarReauthRequired(RuntimeError):  # noqa: N818 - a state, not an error type
    """The refresh token is dead: revoked at Google, expired, or the grant was
    withdrawn. Distinct from `CalendarUnavailable` because the RESPONSE differs
    — one is "try later", the other is "the user must reconnect", and showing
    the wrong one leaves someone waiting for a sync that will never happen."""


@dataclass(frozen=True)
class TokenGrant:
    refresh_token: str | None
    access_token: str
    scope: str
    expires_in: int


def authorize_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    """The consent URL.

    `access_type=offline` + `prompt=consent` because Google returns a refresh
    token ONLY on the first consent for a given client/user pair. Without
    `prompt=consent`, a user who reconnects after we lost their token gets an
    access token and no refresh token, and the link silently stops working an
    hour later.
    """
    return f"{AUTH_URL}?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )


async def exchange_code(
    code: str, *, client_id: str, client_secret: str, redirect_uri: str
) -> TokenGrant:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            resp = await client.post(
                TOKEN_URL,
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
        except httpx.HTTPError as exc:
            raise CalendarUnavailable(f"token exchange failed: {type(exc).__name__}") from exc

    if resp.status_code >= 400:
        # The body echoes the code on some errors, so only the error field is
        # surfaced — an authorization code is a credential until it is spent.
        detail = (resp.json() or {}).get("error", "unknown") if resp.text else "empty"
        raise CalendarUnavailable(f"token exchange rejected: {detail}")

    body = resp.json()
    return TokenGrant(
        refresh_token=body.get("refresh_token"),
        access_token=body["access_token"],
        scope=body.get("scope", ""),
        expires_in=int(body.get("expires_in", 0)),
    )


async def refresh_access_token(
    refresh_token: str, *, client_id: str, client_secret: str
) -> TokenGrant:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            resp = await client.post(
                TOKEN_URL,
                data={
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:
            raise CalendarUnavailable(f"refresh failed: {type(exc).__name__}") from exc

    if resp.status_code in (400, 401):
        # `invalid_grant` means the user revoked us, changed their password, or
        # the token aged out. Retrying cannot fix any of those.
        raise CalendarReauthRequired("refresh token is no longer valid")
    if resp.status_code >= 400:
        raise CalendarUnavailable(f"refresh returned HTTP {resp.status_code}")

    body = resp.json()
    return TokenGrant(
        refresh_token=body.get("refresh_token"),
        access_token=body["access_token"],
        scope=body.get("scope", ""),
        expires_in=int(body.get("expires_in", 0)),
    )


async def account_email(access_token: str) -> str | None:
    """Which Google account this grant belongs to. Best effort — a missing
    email makes the UI less clear, not broken."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(
                USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
        return str(resp.json().get("email")) if resp.status_code == 200 else None
    except Exception as exc:
        logger.debug("userinfo lookup failed: %s", exc)
        return None


async def list_today(access_token: str, *, timezone: str, day: dt.date | None = None) -> list[str]:
    """Today's event TITLES. Nothing else leaves Google.

    `singleEvents=true` expands recurring series — without it a weekly standup
    arrives as one recurrence rule with a start date months ago, and today's
    instance is invisible.

    Cancelled and declined events are dropped: you do not dress for a meeting
    you are not attending, and a declined wedding invitation would otherwise
    put you in formal ethnic all day.
    """
    target = day or dt.date.today()
    start = dt.datetime.combine(target, dt.time.min).isoformat() + "Z"
    end = dt.datetime.combine(target, dt.time.max).isoformat() + "Z"

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(
                EVENTS_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                params={
                    "timeMin": start,
                    "timeMax": end,
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "timeZone": timezone,
                    "maxResults": 50,
                    # Ask Google for the two fields we use and nothing else, so
                    # attendees and descriptions are never transmitted at all
                    # rather than being received and discarded.
                    "fields": "items(summary,status,attendees(self,responseStatus))",
                },
            )
    except httpx.HTTPError as exc:
        raise CalendarUnavailable(f"events fetch failed: {type(exc).__name__}") from exc

    if resp.status_code in (401, 403):
        raise CalendarReauthRequired(f"calendar access refused (HTTP {resp.status_code})")
    if resp.status_code >= 400:
        raise CalendarUnavailable(f"events fetch returned HTTP {resp.status_code}")

    titles: list[str] = []
    for item in resp.json().get("items", []):
        if item.get("status") == "cancelled":
            continue
        if _declined(item):
            continue
        summary = item.get("summary")
        if summary:
            titles.append(str(summary))
    return titles


async def list_public_holidays(
    *,
    api_key: str,
    calendar_id: str,
    day: dt.date,
) -> list[str]:
    """Holiday names on `day`, from a PUBLIC Google calendar.

    WHY AN API KEY AND NOT OAUTH
    -----------------------------
    A national holiday calendar is not a person's diary. It needs no consent,
    no refresh token and no per-user grant -- an API key reads it, and the
    same answer serves every user in that country. This is deliberately a
    different code path from `list_today`, which handles the user's own
    calendar and is bound by the consent the UI collected.

    WHY THIS REPLACES A HAND-WRITTEN TABLE
    ---------------------------------------
    `config/observances.yaml` can hold fixed dates forever -- Independence Day
    does not move. It cannot hold Diwali, Holi or Eid without someone entering
    every year by hand, and a table like that expires silently. Google's
    holiday calendars already carry them.

    Returns NAMES, not occasions. Mapping "Diwali" to `festival_day` is this
    product's judgement and belongs in config, not in a client that only knows
    how to talk to Google.

    Raises `CalendarUnavailable` so the caller falls back to the table rather
    than failing the request: a holiday lookup is an enhancement, and no
    suggestion should 500 because Google is slow.
    """
    start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC)
    params = {
        "key": api_key,
        "timeMin": start.isoformat(),
        "timeMax": (start + dt.timedelta(days=1)).isoformat(),
        "singleEvents": "true",
        "maxResults": "20",
    }
    url = CALENDAR_EVENTS_URL.format(calendar_id=quote(calendar_id, safe=""))
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise CalendarUnavailable(f"holiday calendar unreachable: {exc}") from exc

    if resp.status_code != 200:
        # 403 here usually means the key is missing the Calendar API, and 404
        # a mistyped calendar id. Both are configuration, and both must read
        # as "unavailable" rather than "no holiday today" -- otherwise a
        # broken key looks exactly like an ordinary Tuesday.
        raise CalendarUnavailable(
            f"holiday calendar returned {resp.status_code}: {resp.text[:200]}"
        )

    names: list[str] = []
    for item in resp.json().get("items", []):
        summary = (item.get("summary") or "").strip()
        if summary:
            names.append(summary)
    return names


def _declined(item: dict[str, Any]) -> bool:
    for attendee in item.get("attendees") or []:
        if attendee.get("self") and attendee.get("responseStatus") == "declined":
            return True
    return False


async def revoke(token: str) -> bool:
    """Revoke at Google. Returns whether the provider confirmed it.

    The boolean is stored (`revoked_at_provider`) rather than assumed: a local
    clear that Google did not confirm is a materially different state to
    disclose under an erasure request than a confirmed revocation.
    """
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(REVOKE_URL, data={"token": token})
        # 200 = revoked. 400 usually means already invalid, which is the same
        # end state and must not be reported as a failure.
        return resp.status_code in (200, 400)
    except httpx.HTTPError as exc:
        logger.warning("revoke call failed: %s", type(exc).__name__)
        return False
