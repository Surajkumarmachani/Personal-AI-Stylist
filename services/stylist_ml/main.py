"""ONNX inference service (Step 3.1).

    POST /segment   image bytes            -> masks + slot hints + coverage
    POST /matte     image bytes [+ mask]   -> RGBA cutout PNG
    POST /embed     image bytes            -> 768-d unit vector
    GET  /models                           -> names, versions, checksums
    GET  /readyz                           -> per-model load state

NO DATABASE, NO OBJECT STORE, NO CREDENTIALS — by design and permanently.
This service scales on CPU-seconds while workers scale on I/O concurrency, so
coupling them means buying CPU (later GPU) to sit waiting on S3 (View 2). It is
also the only process that handles user pixels through a model, and it holds
nothing that could read the wardrobe: a compromise here cannot enumerate
anyone's clothes.

WHY BYTES IN THE BODY RATHER THAN THE PLAN'S {image_url}
--------------------------------------------------------
Step 3.1 specifies `{image_url}` for each endpoint. That would require giving
this service object-store credentials, which is exactly the blast radius the
paragraph above is trying to avoid — and it would put an S3 round trip inside
the latency budget of a service whose own p95 target is 800ms. The worker
already holds the bytes when it calls: it read them to validate them. Passing
them along costs nothing and keeps this service credential-free.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status

from stylist_ml import embedding, matting, moderation, registry, segmentation
from stylist_ml.runtime import LoadedModel, ModelUnavailable, load

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 12 * 1024 * 1024

# HOW MANY INFERENCES THIS PROCESS WILL RUN AT ONCE.
#
# A bulkhead (§C2), and it was not optional. Without it, worker concurrency
# multiplies straight onto one inference process: 4 workers x 4 onnxruntime
# intra-op threads = 16 threads competing for 8 vCPUs. Every request slows,
# the client's read timeout fires, the stage retries, and the retry adds more
# load — a saturation collapse. Observed directly: matte ReadTimeouts with
# `stage_attempts={"matte": 2}` and the queue climbing past 200 while four
# workers sat retrying.
#
# Over the limit we return 503 with Retry-After rather than queueing
# indefinitely. That composes with the worker's Unavailable handling: it waits
# without consuming the image's retry budget, so backpressure costs latency
# instead of losing uploads.
#
# MEASURED, not provisional. 2 slots x 2 ORT threads = 4 busy threads on an
# 8-vCPU box. At 4x4 the service thrashed and every request read-timed-out;
# at this setting a 5-photo burst drained with ZERO read timeouts
# (2026-09-10). POD COUNTS are still unmeasured — that part needs traffic.
MAX_CONCURRENT_INFERENCE = int(os.environ.get("ML_MAX_CONCURRENCY", "2"))
_inference_slots = asyncio.Semaphore(MAX_CONCURRENT_INFERENCE)

# How long to wait for a slot before shedding. Short on purpose: telling the
# caller "busy, come back" beats holding its connection while its own read
# timeout runs down.
SLOT_WAIT_SECONDS = 5.0
RETRY_AFTER_SECONDS = 5


@asynccontextmanager
async def _inference_slot(kind: str) -> AsyncIterator[None]:
    try:
        await asyncio.wait_for(_inference_slots.acquire(), timeout=SLOT_WAIT_SECONDS)
    except TimeoutError:
        logger.warning("%s shed: all %d inference slots busy", kind, MAX_CONCURRENT_INFERENCE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"all {MAX_CONCURRENT_INFERENCE} inference slots busy",
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        ) from None
    try:
        yield
    finally:
        _inference_slots.release()


# name -> loaded model. Populated at startup; a model whose weights are absent
# is simply missing from this dict and reported unready rather than crashing
# the process.
_models: dict[str, LoadedModel] = {}


def _load_all() -> None:
    for spec in (registry.SEGFORMER, registry.FASHION_SIGLIP, registry.NSFW):
        try:
            _models[spec.name] = load(spec)
        except ModelUnavailable as exc:
            logger.warning("%s unavailable: %s", spec.name, exc)
        except Exception:
            logger.exception("%s failed to load", spec.name)
    # u2net loads through rembg, which owns its own cache layout.
    matting.warm()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load every model before accepting traffic.

    In a thread because building three ONNX sessions is tens of seconds of
    blocking CPU work, and doing it on the event loop makes the health endpoint
    unresponsive for the whole load — which an orchestrator reads as a failed
    start and restarts, forever.
    """
    logging.basicConfig(level=logging.INFO)
    await asyncio.to_thread(_load_all)
    yield
    _models.clear()


app = FastAPI(title="Stylist ML Inference", version="0.3.0", lifespan=lifespan)


def _require(name: str) -> LoadedModel:
    model = _models.get(name)
    if model is None:
        # 503, not 500: "not ready yet" is the truth, and the worker's retry
        # with backoff is the correct response to it.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{name} is not loaded; see /readyz",
        )
    return model


async def _body(request: Request) -> bytes:
    data = await request.body()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="empty body")
    if len(data) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"body is {len(data)} bytes, limit {MAX_BODY_BYTES}",
        )
    return data


# --------------------------------------------------------------------- ops


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness only — deliberately does NOT check the models.

    Failing liveness on a missing weight file would restart a pod that cannot
    fix the problem by restarting, turning a mount misconfiguration into a
    crash loop.
    """
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(response: Response) -> dict[str, Any]:
    """Ready means every session is BUILT, not that the files exist.

    Reporting ready on file presence would route traffic to a pod with tens of
    seconds of loading still ahead of it, and the first request would blow the
    latency budget.
    """
    states: dict[str, str] = {}
    for spec in registry.ALL_MODELS:
        if spec.task == "matte":
            states[spec.name] = "loaded" if matting.is_loaded() else "missing"
        elif spec.name in _models:
            states[spec.name] = "loaded"
        else:
            states[spec.name] = "present_not_loaded" if spec.available() else "missing"

    ready = all(v == "loaded" for v in states.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"ready": ready, "models": states}


@app.get("/models")
async def models() -> dict[str, Any]:
    """Inventory with checksums, per §D1's registry requirement.

    The checksum is of the bytes actually loaded, so when an accuracy
    regression surfaces weeks later the question "which weights served that
    request" has an answer.
    """
    entries: list[dict[str, Any]] = []
    for spec in registry.ALL_MODELS:
        loaded = _models.get(spec.name)
        entry: dict[str, Any] = {
            "name": spec.name,
            "task": spec.task,
            "repo": spec.repo,
            "revision": spec.revision,
            "loaded": loaded is not None or (spec.task == "matte" and matting.is_loaded()),
            "path": str(spec.path),
        }
        if loaded is not None:
            entry["sha256"] = loaded.sha256
            entry["bytes"] = spec.path.stat().st_size
            entry["input"] = loaded.input_name
            entry["outputs"] = list(loaded.output_names)
        elif spec.task == "matte" and matting.is_loaded():
            # u2net loads through rembg rather than runtime.load(), so its
            # digest comes from the matting module instead of a LoadedModel.
            entry["sha256"] = matting.loaded_sha256()
            entry["bytes"] = spec.path.stat().st_size
        elif spec.available():
            entry["bytes"] = spec.path.stat().st_size
        if spec.notes:
            entry["notes"] = spec.notes
        entries.append(entry)
    return {"models": entries, "embedding_dim": registry.EMBEDDING_DIM}


# --------------------------------------------------------------- inference


@app.post("/segment")
async def segment(request: Request) -> dict[str, Any]:
    """Per-class garment masks for one image.

    JSON with base64 masks, unlike /matte which returns raw PNG bytes. The
    difference is deliberate: a response with N masks needs an envelope, and
    multipart for a handful of 1-bit PNGs is more machinery than the 33% base64
    overhead costs. /matte returns exactly one large image, where that overhead
    would be the dominant cost.

    `slot_hint` comes from taxonomy.yaml's atr_to_slot so the mapping lives in
    one place — but `atr_label` is returned alongside it so a caller is never
    locked into our interpretation of the model's output.
    """
    data = await _body(request)
    model = _require(registry.SEGFORMER.name)

    try:
        async with _inference_slot("segment"):
            result = await asyncio.to_thread(segmentation.segment, model, data)
    except HTTPException:
        # Let deliberate HTTP responses through untouched.
        #
        # The bulkhead raises HTTPException(503, Retry-After) to shed load, and
        # HTTPException IS an Exception — so the broad handler below was
        # catching it and rewriting it as a 500. The client then saw a server
        # error instead of backpressure, spent the image's retry budget on it,
        # and DLQ'd the job: `segment failed after 3 attempts: HTTPStatusError
        # 500`. The load shedding worked; the error handler destroyed the
        # signal.
        raise
    except Exception as exc:
        logger.exception("segment failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"segment failed: {type(exc).__name__}",
        ) from exc

    from stylist_domain.taxonomy import load_taxonomy

    taxonomy = load_taxonomy()
    return {
        "width": result.width,
        "height": result.height,
        "model": result.model,
        "skin_pct": result.skin_pct,
        "non_garment_coverage": result.non_garment_coverage,
        "masks": [
            {
                "atr_class": m.atr_class,
                "atr_label": m.atr_label,
                "slot_hint": taxonomy.slot_for_atr_class(m.atr_label),
                "area_pct": m.area_pct,
                "bbox": list(m.bbox),
                "mask_png_b64": base64.b64encode(m.mask_png).decode("ascii"),
            }
            for m in result.masks
        ],
        # Spatially disjoint garments, from the masks' PIXELS rather than their
        # labels. Returned ALWAYS rather than only for flat-lays: this service
        # reports what it saw and the caller decides what to do with it, the
        # same principle as returning every mask unfiltered. Costs a connected-
        # component pass over an array we already have, not another inference.
        "flatlay_components": segmentation.flatlay_components(
            list(result.masks), result.width, result.height
        ),
        # A HINT, not truth: the VLM tagging stage may override a mask-derived
        # slot, and a user correction always wins (taxonomy.yaml).
        "slot_hint_confidence": taxonomy.slot_hint_confidence,
    }


@app.post("/matte")
async def matte(request: Request) -> Response:
    """Raw image bytes in, RGBA cutout PNG out.

    An optional `X-Mask-PNG-B64` header restricts matting to one garment from a
    multi-garment frame. A header rather than a JSON envelope so the common
    case — no mask — stays a raw byte body with no encoding overhead on the hot
    path.
    """
    data = await _body(request)

    mask_b64 = request.headers.get("X-Mask-PNG-B64")
    mask: bytes | None = None
    if mask_b64:
        try:
            mask = base64.b64decode(mask_b64, validate=True)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-Mask-PNG-B64 is not valid base64",
            ) from exc

    try:
        async with _inference_slot("matte"):
            result = await asyncio.to_thread(matting.matte, data, mask)
    except matting.ModelUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except HTTPException:
        # Let deliberate HTTP responses through untouched.
        #
        # The bulkhead raises HTTPException(503, Retry-After) to shed load, and
        # HTTPException IS an Exception — so the broad handler below was
        # catching it and rewriting it as a 500. The client then saw a server
        # error instead of backpressure, spent the image's retry budget on it,
        # and DLQ'd the job: `segment failed after 3 attempts: HTTPStatusError
        # 500`. The load shedding worked; the error handler destroyed the
        # signal.
        raise
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


@app.post("/moderate")
async def moderate(request: Request) -> dict[str, Any]:
    """NSFW verdict for one image, computed IN-VPC (Step 4.4).

    Called before any stage that could export pixels, so a quarantined upload
    never reaches a third party. Returns a score and a verdict rather than a
    bare boolean: the caller needs the number for the audit record, and the
    two-threshold band means "flagged" and "quarantined" are different
    outcomes with different consequences.
    """
    data = await _body(request)
    model = _require(registry.NSFW.name)

    try:
        async with _inference_slot("moderate"):
            result = await asyncio.to_thread(moderation.moderate, model, data)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("moderate failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"moderate failed: {type(exc).__name__}",
        ) from exc

    return {
        "nsfw_score": result.nsfw_score,
        "verdict": result.verdict,
        "model": result.model,
        "thresholds": {
            "quarantine": moderation.QUARANTINE_THRESHOLD,
            "review": moderation.REVIEW_THRESHOLD,
        },
    }


@app.post("/embed")
async def embed(request: Request) -> dict[str, Any]:
    """768-d unit vector for one garment image.

    Best results come from a CUTOUT rather than an original: the embedding of a
    shirt photographed on a bed encodes the bed too, and two shirts on
    different backgrounds end up further apart than two different shirts on the
    same one.
    """
    data = await _body(request)
    model = _require(registry.FASHION_SIGLIP.name)

    try:
        async with _inference_slot("embed"):
            result = await asyncio.to_thread(embedding.embed, model, data)
    except ValueError as exc:
        # Dimension or zero-vector problems are contract violations worth
        # surfacing verbatim — they mean the model and the schema disagree.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except HTTPException:
        # Let deliberate HTTP responses through untouched.
        #
        # The bulkhead raises HTTPException(503, Retry-After) to shed load, and
        # HTTPException IS an Exception — so the broad handler below was
        # catching it and rewriting it as a 500. The client then saw a server
        # error instead of backpressure, spent the image's retry budget on it,
        # and DLQ'd the job: `segment failed after 3 attempts: HTTPStatusError
        # 500`. The load shedding worked; the error handler destroyed the
        # signal.
        raise
    except Exception as exc:
        logger.exception("embed failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"embed failed: {type(exc).__name__}",
        ) from exc

    return {"vector": list(result.vector), "dim": result.dim, "model": result.model}
