"""Firebase Cloud Messaging, HTTP v1 (Phase 8's `w-notify`).

WRITTEN AGAINST THE HTTP API, NOT firebase-admin
-------------------------------------------------
`firebase-admin` brings ~20 transitive dependencies and a credential-discovery
layer that reads ambient environment and metadata endpoints — behaviour you do
not want in a container that should have exactly one way to be authenticated.
What is needed here is a signed JWT, a token exchange, and one POST per device.
Same reasoning as `google_calendar.py`.

THE CREDENTIAL IS A PRIVATE KEY, AND IT IS READ FROM A FILE
------------------------------------------------------------
A service-account JSON can act AS the Firebase project. It is read from a path
(`FIREBASE_CREDENTIALS_FILE`) rather than pasted into an env var because a
multi-line PEM inside an env var gets mangled by every shell, .env parser and
CI secret store in a different way — and the usual "fix" is to strip the
newlines, which produces an unparseable key and a stack trace three layers
down. The file is gitignored (`secrets/`).

Nothing in this module logs the key, the token, or a device registration token.

A DEAD TOKEN IS DELETED, NOT RETRIED
-------------------------------------
FCM returns `UNREGISTERED`/`INVALID_ARGUMENT` for a token whose app was
uninstalled, and expects the sender to drop it. Retrying one costs quota every
morning for a device that no longer exists, so `SendResult.permanent_failure`
tells the caller which tokens to disable rather than reschedule.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"

TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# Google caps service-account assertions at 1 hour. Refreshed at 55 minutes so
# a send never races the expiry — an access token that expires mid-batch fails
# half the notifications for a reason that looks random.
TOKEN_LIFETIME_S = 3600
TOKEN_REFRESH_MARGIN_S = 300


class PushUnavailable(RuntimeError):  # noqa: N818 - a state, not an error type
    """FCM is unreachable or refused the credential. Retryable; not a verdict
    on any particular device."""


@dataclass(frozen=True)
class SendResult:
    """Per-token outcome. `permanent_failure` is the set to DELETE."""

    delivered: tuple[str, ...]
    permanent_failure: tuple[tuple[str, str], ...]  # (token, reason)
    transient_failure: tuple[str, ...]

    @property
    def ok(self) -> int:
        return len(self.delivered)


@dataclass(frozen=True)
class ServiceAccount:
    project_id: str
    client_email: str
    private_key: str

    @classmethod
    def from_file(cls, path: str | Path) -> ServiceAccount:
        raw = json.loads(Path(path).read_text())
        missing = [k for k in ("project_id", "client_email", "private_key") if not raw.get(k)]
        if missing:
            # Named explicitly: the usual cause is pointing at the wrong JSON
            # (an OAuth client config rather than a service account), and
            # "KeyError: 'private_key'" does not say that.
            raise ValueError(
                f"{path} is missing {missing}; expected a service-account JSON "
                "(Firebase Console -> Project settings -> Service accounts)"
            )
        return cls(
            project_id=str(raw["project_id"]),
            client_email=str(raw["client_email"]),
            private_key=str(raw["private_key"]),
        )


class FCMClient:
    """One client per process. Caches the access token across sends."""

    def __init__(self, account: ServiceAccount) -> None:
        self._account = account
        self._token: str | None = None
        self._expires_at = 0.0

    async def _access_token(self) -> str:
        now = time.time()
        if self._token and now < self._expires_at - TOKEN_REFRESH_MARGIN_S:
            return self._token

        assertion = jwt.encode(
            {
                "iss": self._account.client_email,
                "scope": FCM_SCOPE,
                "aud": TOKEN_URL,
                "iat": int(now),
                "exp": int(now) + TOKEN_LIFETIME_S,
            },
            self._account.private_key,
            algorithm="RS256",
        )

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(
                    TOKEN_URL, data={"grant_type": JWT_BEARER, "assertion": assertion}
                )
        except httpx.HTTPError as exc:
            raise PushUnavailable(f"token exchange failed: {type(exc).__name__}") from exc

        if resp.status_code >= 400:
            # The body can echo the assertion; only the error code is surfaced.
            detail = (resp.json() or {}).get("error", "unknown") if resp.text else "empty"
            raise PushUnavailable(f"token exchange rejected: {detail}")

        body = resp.json()
        self._token = str(body["access_token"])
        self._expires_at = now + int(body.get("expires_in", TOKEN_LIFETIME_S))
        return self._token

    async def send(
        self,
        tokens: list[str],
        *,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
    ) -> SendResult:
        """One notification to many devices.

        FCM v1 has no multicast endpoint — the old `/batch` was retired — so
        this is one request per token, sequentially. That is fine at this
        scale: a daily digest is a handful of devices per user, and doing it
        concurrently would need a semaphore to avoid the same thundering-herd
        problem the ml service hit in Phase 3.
        """
        if not tokens:
            return SendResult((), (), ())

        access = await self._access_token()
        url = f"https://fcm.googleapis.com/v1/projects/{self._account.project_id}/messages:send"
        headers = {"Authorization": f"Bearer {access}", "Content-Type": "application/json"}

        delivered: list[str] = []
        permanent: list[tuple[str, str]] = []
        transient: list[str] = []

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            for token in tokens:
                payload: dict[str, Any] = {
                    "message": {
                        "token": token,
                        "notification": {"title": title, "body": body},
                        **({"data": data} if data else {}),
                    }
                }
                try:
                    resp = await client.post(url, headers=headers, json=payload)
                except httpx.HTTPError as exc:
                    logger.info("push transport error: %s", type(exc).__name__)
                    transient.append(token)
                    continue

                if resp.status_code == 200:
                    delivered.append(token)
                elif resp.status_code in (400, 403, 404):
                    # 404 UNREGISTERED: app uninstalled. 400 INVALID_ARGUMENT:
                    # malformed or foreign token. 403: this token is not ours.
                    # None of these can be fixed by trying again tomorrow.
                    permanent.append((token, _reason(resp)))
                else:
                    # 429 and 5xx are FCM asking us to back off.
                    transient.append(token)

        return SendResult(tuple(delivered), tuple(permanent), tuple(transient))


def _reason(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        details = body.get("error", {}).get("details") or []
        for detail in details:
            if "errorCode" in detail:
                return str(detail["errorCode"])[:64]
        return str(body.get("error", {}).get("status", resp.status_code))[:64]
    except Exception:
        return f"http_{resp.status_code}"
