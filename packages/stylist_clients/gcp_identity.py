"""Google-signed identity tokens for calling a private Cloud Run service.

In the budget deployment (infra/vm/) the ml service runs on Cloud Run with
public access REMOVED: it holds no data, but an open URL is 4 vCPUs anyone can
spend on the project's bill. Cloud Run then admits only requests carrying an ID
token for an identity with `roles/run.invoker`, which the VM's service
account has.

The token comes from the Compute Engine metadata server, so there is no key
file and no extra dependency. Containers on the VM reach it through Docker's
default bridge, which is why the address is the link-local IP rather than the
`metadata.google.internal` name: the IP needs no DNS inside a container.

When no audience is configured (compose, CI, tests) this returns no headers
and never touches the network, so local behaviour is unchanged.
"""

from __future__ import annotations

import time

import httpx

METADATA_IDENTITY_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/identity"
)
# Google ID tokens live one hour. Refresh well before that so a token cannot
# expire between being read here and arriving at Cloud Run.
TOKEN_TTL_SECONDS = 50 * 60

_cache: dict[str, tuple[str, float]] = {}


async def _fetch_token(audience: str) -> str:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(
            METADATA_IDENTITY_URL,
            params={"audience": audience, "format": "full"},
            headers={"Metadata-Flavor": "Google"},
        )
    resp.raise_for_status()
    return resp.text.strip()


async def auth_headers(audience: str | None) -> dict[str, str]:
    """`Authorization: Bearer <id token>` for `audience`, or {} when unset."""
    if not audience:
        return {}
    cached = _cache.get(audience)
    now = time.monotonic()
    if cached is None or cached[1] <= now:
        token = await _fetch_token(audience)
        cached = (token, now + TOKEN_TTL_SECONDS)
        _cache[audience] = cached
    return {"Authorization": f"Bearer {cached[0]}"}
