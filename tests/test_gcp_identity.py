"""ID-token headers for a private Cloud Run ml service.

No network: the metadata-server fetch is replaced. What matters is that an
unset audience adds nothing (local and CI unchanged), that a set one yields a
Bearer header, and that the token is cached rather than fetched per call — the
worker calls ml several times per photo.
"""

from __future__ import annotations

import pytest

from stylist_clients import gcp_identity
from stylist_clients.ml_client import MLClient

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def fake_metadata(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def fetch(audience: str) -> str:
        calls.append(audience)
        return f"token-for-{audience}"

    monkeypatch.setattr(gcp_identity, "_fetch_token", fetch)
    monkeypatch.setattr(gcp_identity, "_cache", {})
    return calls


async def test_no_audience_means_no_header_and_no_fetch(fake_metadata: list[str]) -> None:
    assert await gcp_identity.auth_headers(None) == {}
    assert await gcp_identity.auth_headers("") == {}
    assert fake_metadata == []


async def test_audience_yields_bearer_token_cached(fake_metadata: list[str]) -> None:
    aud = "https://stylist-ml-1.asia-south1.run.app"
    first = await gcp_identity.auth_headers(aud)
    second = await gcp_identity.auth_headers(aud)
    assert first == second == {"Authorization": f"Bearer token-for-{aud}"}
    assert fake_metadata == [aud]


async def test_expired_token_is_refetched(
    fake_metadata: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    aud = "https://ml.example"
    await gcp_identity.auth_headers(aud)
    token, _ = gcp_identity._cache[aud]
    gcp_identity._cache[aud] = (token, 0.0)
    await gcp_identity.auth_headers(aud)
    assert fake_metadata == [aud, aud]


async def test_ml_client_merges_auth_with_content_type(fake_metadata: list[str]) -> None:
    client = MLClient("https://ml.example/", auth_audience="https://ml.example")
    headers = await client._headers({"Content-Type": "application/octet-stream"})
    assert headers == {
        "Content-Type": "application/octet-stream",
        "Authorization": "Bearer token-for-https://ml.example",
    }
    assert await MLClient("http://ml:8000")._headers() == {}
