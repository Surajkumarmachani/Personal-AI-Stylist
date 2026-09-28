"""Connect Gmail: read store order emails from the user's own inbox (Phase 15).

    GET    /gmail/connect   the Google consent URL
    GET    /gmail/status    connected? which account? last synced?
    DELETE /gmail           stop reading the inbox

The one-click alternative to forwarding (routers/purchases.py). The emails it
finds go through the same intake and the same worker as forwarded ones.

ONE REDIRECT URI FOR BOTH GOOGLE FEATURES
-----------------------------------------
Google redirects to /calendar/callback for Gmail too: the signed `state`
carries `purpose: gmail`, and the calendar callback hands off to
`finish_connect` below. So the OAuth client needs no second registered URI.

DISCONNECT MUST NOT TAKE THE CALENDAR WITH IT
---------------------------------------------
Revoking a token at Google revokes the app's whole grant for that user, Calendar
included. So when the Calendar is still connected, Disconnect forgets the Gmail
token here and says so, rather than silently breaking the calendar suggestions.
"""

from __future__ import annotations

import logging
import urllib.parse
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from stylist_api.deps import CurrentUser, SettingsDep, TenantDB
from stylist_clients import google_calendar as gcal
from stylist_clients import google_gmail
from stylist_db.session import tenant_session

logger = logging.getLogger(__name__)
router = APIRouter(tags=["purchases"])

PROVIDER = "google_gmail"


def _back_to_app(settings: Any, *, connected: bool, **extra: Any) -> RedirectResponse:
    params = {"gmail": "connected" if connected else "failed"}
    params.update({k: str(v) for k, v in extra.items() if v})
    return RedirectResponse(
        url=f"{settings.web_base_url.rstrip('/')}/profile?{urllib.parse.urlencode(params)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/gmail/connect")
async def connect(user: CurrentUser, settings: SettingsDep) -> dict[str, Any]:
    from stylist_api.routers.calendar import _issue_state

    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google sign-in is not configured on this deployment",
        )
    return {
        "authorize_url": gcal.authorize_url(
            client_id=settings.google_client_id,
            redirect_uri=settings.google_redirect_uri,
            state=_issue_state(user.id, settings, purpose="gmail"),
            scopes=google_gmail.SCOPES,
        ),
        "scopes": list(google_gmail.SCOPES),
    }


async def finish_connect(user_id: uuid.UUID, code: str, settings: Any) -> RedirectResponse:
    """The Gmail half of /calendar/callback."""
    grant = await gcal.exchange_code(
        code,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        redirect_uri=settings.google_redirect_uri,
    )
    if not grant.refresh_token:
        return _back_to_app(settings, connected=False, reason="no_refresh_token")
    # A user may untick the Gmail box on the consent screen; a grant without it
    # would fail at the first sync with nothing pointing at why.
    if google_gmail.SCOPES[0] not in grant.scope:
        return _back_to_app(settings, connected=False, reason="gmail_permission_not_granted")

    email = await gcal.account_email(grant.access_token)
    async with tenant_session(user_id) as db:
        await db.execute(
            text(
                """
                INSERT INTO calendar_link
                    (id, user_id, provider, refresh_token, scope, account_email)
                VALUES (:id, :uid, :provider, :token, :scope, :email)
                ON CONFLICT (user_id, provider) DO UPDATE SET
                    refresh_token = EXCLUDED.refresh_token,
                    scope = EXCLUDED.scope,
                    account_email = EXCLUDED.account_email,
                    connected_at = now(),
                    last_synced_at = NULL,
                    revoked_at = NULL,
                    revoked_at_provider = false
                """
            ),
            {
                "id": uuid.uuid4(),
                "uid": user_id,
                "provider": PROVIDER,
                "token": grant.refresh_token,
                "scope": grant.scope,
                "email": email,
            },
        )
    return _back_to_app(settings, connected=True, account=email)


@router.get("/gmail/status")
async def gmail_status(user: CurrentUser, db: TenantDB, settings: SettingsDep) -> dict[str, Any]:
    row = (
        (
            await db.execute(
                text(
                    "SELECT account_email, last_synced_at, revoked_at, "
                    "refresh_token IS NOT NULL AS has_token "
                    "FROM calendar_link WHERE provider = :p"
                ),
                {"p": PROVIDER},
            )
        )
        .mappings()
        .one_or_none()
    )
    available = bool(settings.google_client_id and settings.google_client_secret)
    if row is None:
        return {"available": available, "connected": False, "reconnect_required": False}
    return {
        "available": available,
        "connected": bool(row["has_token"]) and row["revoked_at"] is None,
        # The sync found the grant dead (revoked, expired, or a Testing-mode
        # 7-day token): the user must press Connect again.
        "reconnect_required": not row["has_token"] and row["revoked_at"] is not None,
        "account_email": row["account_email"],
        "last_synced_at": row["last_synced_at"].isoformat() if row["last_synced_at"] else None,
    }


@router.delete("/gmail")
async def disconnect(user: CurrentUser) -> dict[str, Any]:
    async with tenant_session(user.id) as db:
        token = (
            await db.execute(
                text("SELECT refresh_token FROM calendar_link WHERE provider = :p"),
                {"p": PROVIDER},
            )
        ).scalar()
        if not token:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Gmail is not connected"
            )
        calendar_live = (
            await db.execute(
                text(
                    "SELECT 1 FROM calendar_link WHERE provider = 'google' "
                    "AND refresh_token IS NOT NULL AND revoked_at IS NULL"
                )
            )
        ).scalar()
        confirmed = False if calendar_live else await gcal.revoke(token)
        await db.execute(
            text(
                "UPDATE calendar_link SET refresh_token = NULL, revoked_at = now(), "
                "revoked_at_provider = :c WHERE provider = :p"
            ),
            {"c": confirmed, "p": PROVIDER},
        )
    return {
        "disconnected": True,
        "revoked_at_google": confirmed,
        "note": (
            "Your calendar is still connected, so Google keeps one grant for this app; "
            "we have deleted the Gmail token and will not read your inbox again."
            if calendar_live
            else None
        ),
    }
