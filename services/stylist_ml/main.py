"""ONNX inference service.

PHASE 2 SCOPE: /matte only. /segment and /embed land in Phase 3.

This service has NO DATABASE ACCESS, by design and permanently. It scales on
CPU/GPU-seconds while workers scale on I/O concurrency; coupling them means
buying GPU to wait on S3 (View 2). It is also the only place user pixels are
processed by a model, and it holds no credentials that could read the wardrobe —
so a compromise here cannot enumerate anyone's clothes.

Bytes are POSTed in the request body rather than passed as an object-store key
on purpose: this service gets no storage credentials either, which keeps the
blast radius to the single image in flight.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status

from stylist_ml import matting

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load the model before accepting traffic.

    Run in a thread: building the ONNX session is ~20s of blocking CPU work,
    and doing it on the event loop would make the health endpoint unresponsive
    for the whole load — which orchestrators read as a failed start.
    """
    logging.basicConfig(level=logging.INFO)
    await asyncio.to_thread(matting.warm)
    yield


app = FastAPI(title="Stylist ML Inference", version="0.2.0", lifespan=lifespan)

MAX_BODY_BYTES = 12 * 1024 * 1024


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness only. Deliberately does NOT check the model: a missing weight
    file is a readiness problem, and failing liveness on it would put the pod
    in a restart loop that can never fix itself."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(response: Response) -> dict[str, Any]:
    """Ready means the session is BUILT, not that the file exists.

    Reporting ready on file presence alone would route traffic to a pod that
    still has a ~20s load ahead of it, and the first request would blow the
    latency budget.
    """
    loaded = matting.is_loaded()
    if not loaded:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "ready": loaded,
        "models": {
            "u2net": "loaded"
            if loaded
            else ("present_not_loaded" if matting.model_available() else "missing")
        },
        "model_path": str(matting.model_path()),
    }


@app.get("/models")
async def models() -> dict[str, Any]:
    """Loaded model names + versions + checksums, per §D1's registry rule.

    Checksums are what let you prove which weights served a given request when
    an accuracy regression shows up weeks later.
    """
    entries = []
    if matting.model_available():
        import hashlib

        path = matting.model_path()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        entries.append(
            {
                "name": matting.MODEL_NAME,
                "task": "matte",
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256_prefix": digest,
            }
        )
    return {"models": entries}


@app.post("/matte")
async def matte(request: Request) -> Response:
    """Raw image bytes in, RGBA cutout PNG out.

    Metadata comes back in headers rather than a JSON envelope so the PNG does
    not need base64 encoding — a 33% size penalty on every image, on the hot
    path, for no benefit.
    """
    body = await request.body()
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="empty body")
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"body is {len(body)} bytes, limit {MAX_BODY_BYTES}",
        )

    try:
        result = matting.matte(body)
    except matting.ModelUnavailable as exc:
        # 503, not 500: this is "not ready", and the worker's retry is the
        # correct response to it.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except Exception as exc:
        logger.exception("matte failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"matte failed: {type(exc).__name__}",
        ) from exc

    return Response(
        content=result.cutout_png,
        media_type="image/png",
        headers={
            "X-Alpha-Coverage": f"{result.alpha_coverage:.6f}",
            "X-Cutout-Width": str(result.width),
            "X-Cutout-Height": str(result.height),
            "X-Matte-Model": result.model,
        },
    )
