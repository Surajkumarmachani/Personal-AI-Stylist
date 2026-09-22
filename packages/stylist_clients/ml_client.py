"""Client for services/stylist_ml.

Timeouts are explicit and finite everywhere. A model service that hangs must
fail the stage so the state machine can retry with backoff; an unbounded wait
would occupy a worker slot indefinitely and, across an onboarding burst,
starve the queue — the failure bulkheads exist to prevent (§C2).

Per-endpoint timeouts rather than one global value, because the work differs by
an order of magnitude: embedding a 224x224 crop is ~0.5s, matting a full frame
on CPU is several seconds. A single timeout would either be too tight for
matting or uselessly loose for embedding.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import httpx

# Measured in-container: embed ~0.5s, segment ~2.6s, matte ~3-5s. Ceilings are
# ~4x those so a slow-but-working call succeeds while a hung one fails fast.
# PROVISIONAL — re-dated 2026-09-17. P9 arrived with no real traffic, so this is unchanged.
# Resolves when: p95/p99 per ml endpoint over a week of real ingests. Today
# every percentile comes from synthetic bursts on one laptop.
CONNECT_TIMEOUT = 5.0
EMBED_TIMEOUT = 20.0
MODERATE_TIMEOUT = 25.0
SEGMENT_TIMEOUT = 45.0
MATTE_TIMEOUT = 60.0


class MLUnavailable(RuntimeError):  # noqa: N818 - a state, not an error type
    """The service is unreachable or still loading.

    Distinct from a request that failed: this one says nothing about the image,
    so the caller should wait rather than count it against the image's retry
    budget. Carries the server's Retry-After when it sent one.
    """

    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


def _raise_if_unavailable(exc: Exception) -> None:
    """Map transport-level failures to MLUnavailable.

    Connect errors and timeouts mean "nobody is listening" or "nobody
    answered" — both are the service being down, not a verdict on the image.
    A read timeout is deliberately NOT included: the service accepted the
    request and then took too long, which can genuinely be this image (a huge
    frame) rather than the service.
    """
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout):
        raise MLUnavailable(f"{type(exc).__name__}: {exc}") from exc


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code in (502, 503, 504):
        retry_after = resp.headers.get("Retry-After")
        raise MLUnavailable(
            f"HTTP {resp.status_code} from ml service",
            retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
        )
    resp.raise_for_status()


@dataclass(frozen=True, slots=True)
class MatteResponse:
    cutout_png: bytes
    alpha_coverage: float
    width: int
    height: int
    model: str
    # Share of the opaque alpha in its largest connected blob; 1.0 is one
    # solid garment. Defaults to 1.0 so an older ml service that does not send
    # the header reads as "not fragmented" rather than tripping the guard on
    # every photo.
    largest_blob_share: float = 1.0


@dataclass(frozen=True, slots=True)
class FlatlayComponent:
    """One spatially disjoint garment in a flat-lay.

    Derived from the union of the masks' pixels, so it carries NO class label —
    on a flat-lay the labels are shape guesses and the whole point of this
    structure is that the pixels are trustworthy while the names are not.
    """

    bbox: tuple[int, int, int, int]
    area_pct: float
    mask_png: bytes


@dataclass(frozen=True, slots=True)
class SegmentMask:
    atr_class: int
    atr_label: str
    # From taxonomy.yaml's atr_to_slot. None for a class with no slot mapping.
    # A HINT: the VLM stage may override it and a user correction always wins.
    slot_hint: str | None
    area_pct: float
    bbox: tuple[int, int, int, int]
    mask_png: bytes


@dataclass(frozen=True, slots=True)
class SegmentResponse:
    masks: tuple[SegmentMask, ...]
    width: int
    height: int
    skin_pct: float
    non_garment_coverage: dict[str, float]
    slot_hint_confidence: float
    model: str
    # Empty when the service did not report any — an older ml image, a frame
    # with nothing in it, or a union that fragmented past the service's cap.
    # A DEFAULT rather than a required field so a version skew between this
    # client and the ml service degrades to the previous behaviour (one
    # whole-frame candidate) instead of raising.
    flatlay_components: tuple[FlatlayComponent, ...] = ()


@dataclass(frozen=True, slots=True)
class EmbedResponse:
    vector: tuple[float, ...]
    dim: int
    model: str


class MLClient:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    def _timeout(self, read: float) -> httpx.Timeout:
        # Short connect, long read: a slow connect means the service is down,
        # not busy, and waiting 60s to discover that wastes a worker slot.
        return httpx.Timeout(read, connect=CONNECT_TIMEOUT)

    async def matte(self, *, image_bytes: bytes, mask_png: bytes | None = None) -> MatteResponse:
        """Background removal. `mask_png` restricts it to one garment."""
        headers = {"Content-Type": "application/octet-stream"}
        if mask_png is not None:
            headers["X-Mask-PNG-B64"] = base64.b64encode(mask_png).decode("ascii")

        try:
            async with httpx.AsyncClient(timeout=self._timeout(MATTE_TIMEOUT)) as client:
                resp = await client.post(
                    f"{self._base_url}/matte", content=image_bytes, headers=headers
                )
        except Exception as exc:
            _raise_if_unavailable(exc)
            raise
        _raise_for_status(resp)
        return MatteResponse(
            cutout_png=resp.content,
            alpha_coverage=float(resp.headers.get("X-Alpha-Coverage", "0")),
            width=int(resp.headers.get("X-Cutout-Width", "0")),
            height=int(resp.headers.get("X-Cutout-Height", "0")),
            model=resp.headers.get("X-Matte-Model", "unknown"),
            largest_blob_share=float(resp.headers.get("X-Largest-Blob-Share", "1")),
        )

    async def segment(self, *, image_bytes: bytes) -> SegmentResponse:
        """Per-class garment masks.

        Returns everything the model saw, unfiltered. Area thresholds, IoU
        de-duplication and L/R shoe merging are Step 3.2 policy and belong to
        the caller — this is a report, not a decision.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout(SEGMENT_TIMEOUT)) as client:
                resp = await client.post(
                    f"{self._base_url}/segment",
                    content=image_bytes,
                    headers={"Content-Type": "application/octet-stream"},
                )
        except Exception as exc:
            _raise_if_unavailable(exc)
            raise
        _raise_for_status(resp)
        body = resp.json()
        return SegmentResponse(
            flatlay_components=tuple(
                FlatlayComponent(
                    bbox=(c["bbox"][0], c["bbox"][1], c["bbox"][2], c["bbox"][3]),
                    area_pct=c["area_pct"],
                    mask_png=base64.b64decode(c["mask_png_b64"]),
                )
                for c in body.get("flatlay_components", ())
            ),
            masks=tuple(
                SegmentMask(
                    atr_class=m["atr_class"],
                    atr_label=m["atr_label"],
                    slot_hint=m["slot_hint"],
                    area_pct=m["area_pct"],
                    bbox=(m["bbox"][0], m["bbox"][1], m["bbox"][2], m["bbox"][3]),
                    mask_png=base64.b64decode(m["mask_png_b64"]),
                )
                for m in body["masks"]
            ),
            width=body["width"],
            height=body["height"],
            skin_pct=body["skin_pct"],
            non_garment_coverage=dict(body["non_garment_coverage"]),
            slot_hint_confidence=body["slot_hint_confidence"],
            model=body["model"],
        )

    async def embed(self, *, image_bytes: bytes) -> EmbedResponse:
        """768-d unit vector.

        Pass a CUTOUT, not an original: the embedding of a shirt photographed
        on a bed encodes the bed too, and two shirts on different backgrounds
        end up further apart than two different shirts on the same one.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout(EMBED_TIMEOUT)) as client:
                resp = await client.post(
                    f"{self._base_url}/embed",
                    content=image_bytes,
                    headers={"Content-Type": "application/octet-stream"},
                )
        except Exception as exc:
            _raise_if_unavailable(exc)
            raise
        _raise_for_status(resp)
        body = resp.json()
        return EmbedResponse(
            vector=tuple(float(v) for v in body["vector"]),
            dim=int(body["dim"]),
            model=body["model"],
        )

    async def moderate(self, *, image_bytes: bytes) -> dict[str, Any]:
        """NSFW verdict, computed in-VPC.

        Called before any stage that could export pixels — the whole reason the
        model is self-hosted rather than a moderation API.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout(MODERATE_TIMEOUT)) as client:
                resp = await client.post(
                    f"{self._base_url}/moderate",
                    content=image_bytes,
                    headers={"Content-Type": "application/octet-stream"},
                )
        except Exception as exc:
            _raise_if_unavailable(exc)
            raise
        _raise_for_status(resp)
        return dict(resp.json())

    async def readyz(self) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=self._timeout(5.0)) as client:
            resp = await client.get(f"{self._base_url}/readyz")
        return dict(resp.json())

    async def models(self) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=self._timeout(10.0)) as client:
            resp = await client.get(f"{self._base_url}/models")
        resp.raise_for_status()
        return dict(resp.json())
