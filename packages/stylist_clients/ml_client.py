"""Client for services/stylist_ml.

Timeouts are explicit and finite. A model service that hangs must fail the
stage so the state machine can retry it with backoff; an unbounded wait would
occupy a worker slot indefinitely and, across a burst, starve the queue — the
failure mode bulkheads exist to prevent (§C2).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True, slots=True)
class MatteResponse:
    cutout_png: bytes
    alpha_coverage: float
    width: int
    height: int
    model: str


class MLClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 60.0) -> None:
        self._base_url = base_url.rstrip("/")
        # Matting a 12MP image on CPU takes seconds, so the read timeout is
        # generous; connect stays short because a slow connect means the
        # service is down, not busy.
        self._timeout = httpx.Timeout(timeout_seconds, connect=5.0)

    async def matte(self, *, image_bytes: bytes) -> MatteResponse:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._base_url}/matte",
                content=image_bytes,
                headers={"Content-Type": "application/octet-stream"},
            )
        resp.raise_for_status()
        return MatteResponse(
            cutout_png=resp.content,
            alpha_coverage=float(resp.headers.get("X-Alpha-Coverage", "0")),
            width=int(resp.headers.get("X-Cutout-Width", "0")),
            height=int(resp.headers.get("X-Cutout-Height", "0")),
            model=resp.headers.get("X-Matte-Model", "unknown"),
        )

    async def readyz(self) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get(f"{self._base_url}/readyz")
        return dict(resp.json())
