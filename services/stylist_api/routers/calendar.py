"""Calendar link and today's occasion (Phase 8).

THE FEATURE IS THE GATE, NOT THE INTEGRATION
---------------------------------------------
Reading a calendar is easy. The plan's requirement is the hard half: classify
the day, and when the classifier is not sure, "fall back to default AND SAY SO
IN THE UI. A confidently wrong occasion is worse than asking."

So `GET /calendar/today` never returns a bare occasion. It returns the
occasion, the confidence, whether that cleared the gate, and a one-line
explanation the UI can render verbatim — together, so a client cannot show the
answer without having the caveat in hand.

EVERY FAILURE DEGRADES TO THE DEFAULT
--------------------------------------
Google down, token revoked, never connected: all of them return 200 with
`is_fallback: true` and a reason. A calendar is an INPUT to getting dressed,
not a dependency of it — an endpoint that 503s because Google is having a
morning would take the whole suggestion flow down with it.

STATE IS SIGNED, NOT STORED
----------------------------
The OAuth `state` parameter is a short-lived JWT carrying the user id. Storing
it in a session table would need cleanup and a session; signing it needs
neither and cannot be replayed after expiry. It is what stops an attacker
completing a consent flow into somebody else's account.
"""

from __future__ import annotations

import logging
import urllib.parse
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from stylist_api.deps import CurrentUser, SettingsDep, TenantDB
from stylist_clients import google_calendar as gcal
from stylist_db.session import tenant_session
from stylist_domain.calendar import classify

logger = logging.getLogger(__name__)

router = APIRouter(tags=["calendar"])

# Long enough to read a consent screen, short enough that a leaked link is
# useless by the time anyone finds it.
STATE_TTL_SECONDS = 600
STATE_AUDIENCE = "calendar-oauth"


def _issue_state(user_id: uuid.UUID, settings: Any) -> str:
    return jwt.encode(
        {
            "sub": str(user_id),
            "aud": STATE_AUDIENCE,
            "exp": datetime.now(UTC) + timedelta(seconds=STATE_TTL_SECONDS),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def _read_state(state: str, settings: Any) -> uuid.UUID:
    try:
        payload = jwt.decode(
            state,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=STATE_AUDIENCE,
        )
        return uuid.UUID(payload["sub"])
    except Exception as exc:
        # Expired, forged, or for a different audience — all the same answer.
        # Distinguishing them in the response would help an attacker more than
        # a user, who only ever sees this by clicking a stale link.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired authorization state; start again",
        ) from exc


def _back_to_app(settings: Any, *, connected: bool, **extra: Any) -> RedirectResponse:
    """Send the browser back to the app, not to a JSON body.

    Google redirects the USER here, so whatever this returns is a page a
    person is looking at. Returning `{"connected": true}` ended a consent flow
    on a raw JSON document with no way back — technically correct and a dead
    end.

    The outcome travels in the query string because the app cannot read this
    response: it is a top-level navigation from Google's domain, not a fetch
    the client made. `account` is the Google address the user just consented
    with — their own, already on screen a moment ago — and no calendar content
    ever appears here.
    """
    params = {"calendar": "connected" if connected else "failed", **{
        k: str(v) for k, v in extra.items() if v
    }}
    query = urllib.parse.urlencode(params)
    return RedirectResponse(
        url=f"{settings.web_base_url.rstrip('/')}/profile?{query}",
        # 303: the callback is a GET but the browser must not re-submit it if
        # the user refreshes the destination — the one-time code is spent.
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/calendar/connect")
async def connect(user: CurrentUser, settings: SettingsDep) -> dict[str, Any]:
    """The Google consent URL to send the user to."""
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="calendar is not configured on this deployment",
        )
    return {
        "authorize_url": gcal.authorize_url(
            client_id=settings.google_client_id,
            redirect_uri=settings.google_redirect_uri,
            state=_issue_state(user.id, settings),
        ),
        "scopes": list(gcal.SCOPES),
    }


@router.get("/calendar/callback")
async def callback(
    settings: SettingsDep,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
    # A RedirectResponse, not a dict: this is a page the USER lands on after
    # consent, so it has to take them somewhere.
) -> RedirectResponse:
    """Google redirects here. NOT authenticated by a bearer token.

    The browser arriving from Google carries no Authorization header, so the
    `state` JWT is what identifies the user — which is exactly why it is signed
    and expiring rather than an opaque string we would have to trust.
    """
    if error:
        # The user pressed cancel. That is a choice, not a failure.
        return _back_to_app(settings, connected=False, reason=error)
    if not code or not state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="missing code or state")

    user_id = _read_state(state, settings)
    grant = await gcal.exchange_code(
        code,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        redirect_uri=settings.google_redirect_uri,
    )

    if not grant.refresh_token:
        # `prompt=consent` should guarantee one. Without it the link works for
        # an hour and then dies silently, which is worse than refusing now.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google returned no refresh token; disconnect the app in your "
            "Google account settings and try again",
        )

    # VERIFY the grant rather than assuming it. Google lets a user uncheck
    # scopes on the consent screen, and a narrower grant would otherwise show
    # up as a 403 mid-sync with nothing pointing at the cause.
    if gcal.SCOPES[0] not in grant.scope:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="calendar access was not granted; the connection needs the "
            "read-only calendar permission",
        )

    email = await gcal.account_email(grant.access_token)

    async with tenant_session(user_id) as db:
        await db.execute(
            text(
                """
                INSERT INTO calendar_link
                    (id, user_id, provider, refresh_token, scope, account_email)
                VALUES (:id, :uid, 'google', :token, :scope, :email)
                ON CONFLICT (user_id, provider) DO UPDATE SET
                    refresh_token = EXCLUDED.refresh_token,
                    scope = EXCLUDED.scope,
                    account_email = EXCLUDED.account_email,
                    connected_at = now(),
                    revoked_at = NULL,
                    revoked_at_provider = false
                """
            ),
            {
                "id": uuid.uuid4(),
                "uid": user_id,
                "token": grant.refresh_token,
                "scope": grant.scope,
                "email": email,
            },
        )
    return _back_to_app(settings, connected=True, account=email)


@router.get("/calendar/status")
async def link_status(user: CurrentUser, db: TenantDB) -> dict[str, Any]:
    row = await db.execute(
        text(
            "SELECT account_email, scope, connected_at, last_synced_at, revoked_at, "
            "refresh_token IS NOT NULL AS has_token "
            "FROM calendar_link WHERE provider = 'google'"
        )
    )
    link = row.mappings().one_or_none()
    if link is None:
        return {"connected": False}
    return {
        "connected": bool(link["has_token"]),
        "account_email": link["account_email"],
        "scope": link["scope"],
        "connected_at": link["connected_at"],
        "last_synced_at": link["last_synced_at"],
        "revoked_at": link["revoked_at"],
    }


@router.get("/calendar/today")
async def today(user: CurrentUser, db: TenantDB, settings: SettingsDep) -> dict[str, Any]:
    """Today's occasion, with the confidence gate applied.

    ALWAYS 200. Google being down, a revoked token, or no link at all are all
    reported as a fallback with a reason rather than as an error — getting
    dressed must not depend on a third party being up.
    """
    row = await db.execute(
        text(
            "SELECT refresh_token FROM calendar_link "
            "WHERE provider = 'google' AND refresh_token IS NOT NULL"
        )
    )
    token = row.scalar()
    profile = await db.execute(text("SELECT timezone FROM user_profile LIMIT 1"))
    timezone = profile.scalar() or "Asia/Kolkata"

    if not token:
        result = classify([])
        return _payload(result, source="not_connected", reconnect_required=False)

    try:
        grant = await gcal.refresh_access_token(
            token,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
        )
        titles = await gcal.list_today(grant.access_token, timezone=timezone)
    except gcal.CalendarReauthRequired:
        result = classify([])
        return _payload(result, source="reauth_required", reconnect_required=True)
    except gcal.CalendarUnavailable as exc:
        logger.info("calendar unavailable for %s: %s", user.id, exc)
        result = classify([])
        return _payload(result, source="unavailable", reconnect_required=False)

    async with tenant_session(user.id) as write:
        await write.execute(
            text("UPDATE calendar_link SET last_synced_at = now() WHERE provider = 'google'")
        )

    # `events_seen` is a COUNT, never the titles. The classification is
    # returned; the calendar contents are not, and are never persisted.
    result = classify(titles)
    return _payload(result, source="calendar", reconnect_required=False, events_seen=len(titles))


def _payload(result: Any, *, source: str, reconnect_required: bool, **extra: Any) -> dict[str, Any]:
    return {
        "occasion": result.occasion,
        "dress_code": result.dress_code,
        "formality_target": result.formality_target,
        "confidence": round(result.confidence, 2),
        # The gate, as its own flag. A client must not have to know the
        # threshold to decide whether to present this as a finding or a guess.
        "confident": result.confident,
        "is_fallback": result.is_fallback,
        # Rendered verbatim by the UI. Carries no event text.
        "explanation": result.explanation,
        "source": source,
        "reconnect_required": reconnect_required,
        **extra,
    }


@router.delete("/calendar", status_code=status.HTTP_200_OK)
async def disconnect(user: CurrentUser, settings: SettingsDep) -> dict[str, Any]:
    """Revoke at Google, then forget locally.

    In that order, and the outcome of the provider call is RECORDED rather than
    assumed. Clearing our copy first would leave a live grant on Google's side
    that nothing in this system knows about — and "we deleted our token" is not
    the same claim as "the user's calendar is no longer readable by us", which
    is the one an erasure request is actually asking about (§C5).
    """
    async with tenant_session(user.id) as db:
        row = await db.execute(
            text("SELECT refresh_token FROM calendar_link WHERE provider = 'google'")
        )
        token = row.scalar()
        if not token:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="no calendar is connected"
            )

        confirmed = await gcal.revoke(token)
        await db.execute(
            text(
                "UPDATE calendar_link SET refresh_token = NULL, revoked_at = now(), "
                "revoked_at_provider = :confirmed WHERE provider = 'google'"
            ),
            {"confirmed": confirmed},
        )

    return {
        "disconnected": True,
        # Surfaced honestly: a local clear Google did not confirm is a
        # different state, and the user is entitled to know which one happened.
        "revoked_at_provider": confirmed,
        "note": None
        if confirmed
        else "Google did not confirm the revocation; remove access manually at "
        "https://myaccount.google.com/permissions",
    }
