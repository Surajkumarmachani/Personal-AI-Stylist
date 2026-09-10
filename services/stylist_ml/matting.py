"""Background removal via rembg/u2net.

MODEL WEIGHTS COME FROM A MOUNT, NOT THE IMAGE
----------------------------------------------
u2net is ~176MB. Baked into a layer it makes every deploy slow and every model
swap a rebuild — weights are data, not code (View 2). rembg reads `U2NET_HOME`,
so the mount point is configured there and `scripts/download_models.py`
pre-fetches into it.

The consequence is that this service can start without its weights, and it must
be honest about that rather than downloading 176MB from the internet on the
first user request (slow, and a surprise egress from a service whose whole
point is that pixels stay inside the VPC). So: if the model file is absent, the
endpoint reports unavailable and says what to run. `download_on_demand` exists
for local convenience and is off by default.
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

MODEL_NAME = "u2net"
MODEL_HOME = Path(os.environ.get("U2NET_HOME", "/models/u2net"))
DOWNLOAD_ON_DEMAND = os.environ.get("MATTE_DOWNLOAD_ON_DEMAND", "false").lower() == "true"

# Below this the alpha channel is treated as transparent when computing the
# content bounding box. Not 0: u2net leaves a faint halo of very low alpha
# around the subject, and trimming to alpha>0 keeps that halo and defeats the
# crop.
ALPHA_TRIM_THRESHOLD = 16


class ModelUnavailable(RuntimeError):  # noqa: N818 - a state, not an error type
    pass


@dataclass(frozen=True, slots=True)
class MatteResult:
    cutout_png: bytes
    alpha_coverage: float  # fraction of pixels with meaningful alpha
    width: int
    height: int
    model: str


def model_path() -> Path:
    """Locate the weights, wherever rembg decided to put them.

    rembg nests its own `models/<name>/` beneath U2NET_HOME rather than writing
    the file at the root, and that layout is an implementation detail of a
    dependency — it has already changed between releases. Globbing for the
    filename means a rembg upgrade that reorganises the cache does not silently
    turn into "model missing" in production.

    Returns the conventional path when nothing is found, so error messages name
    a concrete location rather than `None`.
    """
    direct = MODEL_HOME / f"{MODEL_NAME}.onnx"
    if direct.is_file():
        return direct
    for found in sorted(MODEL_HOME.rglob(f"{MODEL_NAME}.onnx")):
        return found
    return direct


def model_available() -> bool:
    return model_path().is_file()


_loaded = False


def is_loaded() -> bool:
    """Whether the ONNX session is actually built, not merely present on disk.

    /readyz reports this rather than file existence: a 20-second model load is
    the difference between "the file is there" and "this pod can serve a
    request inside the latency budget".
    """
    return _loaded


def warm() -> bool:
    """Build the session ahead of the first request.

    Loading u2net takes ~20s in a container. Lazily, that entire cost lands on
    whichever user happens to upload first — a 28s ingest against a 10s budget,
    once per pod, looking exactly like a performance bug. Doing it at startup
    moves it off the user-facing path and lets readiness gate traffic until the
    pod can actually serve.

    Returns False (and does not raise) when weights are missing: the container
    should come up and report NOT ready, not crash-loop on a mount problem it
    cannot fix by restarting.
    """
    global _loaded
    try:
        _session()
        _loaded = True
        logger.info("matting model warm: %s", model_path())
    except Exception:
        _loaded = False
        logger.exception("matting model failed to load; service will report not ready")
    return _loaded


@lru_cache(maxsize=1)
def _session():  # type: ignore[no-untyped-def]  # rembg ships no stubs
    """Load the ONNX session once per process.

    lru_cache rather than a module global so the load happens exactly once —
    and so tests can clear it.
    """
    if not model_available() and not DOWNLOAD_ON_DEMAND:
        raise ModelUnavailable(
            f"{model_path()} is missing. Run `python scripts/download_models.py` "
            "on the host and mount ./models into the container. Weights are "
            "deliberately not baked into the image."
        )
    os.environ.setdefault("U2NET_HOME", str(MODEL_HOME))
    MODEL_HOME.mkdir(parents=True, exist_ok=True)
    from rembg import new_session

    logger.info("loading matting model %s from %s", MODEL_NAME, MODEL_HOME)
    return new_session(MODEL_NAME)


def matte(image_bytes: bytes) -> MatteResult:
    """Remove the background and trim to the subject's bounding box."""
    from rembg import remove

    session = _session()
    cutout = remove(image_bytes, session=session)

    with Image.open(io.BytesIO(cutout)) as img:
        rgba = img.convert("RGBA")

        alpha = rgba.getchannel("A")
        total = rgba.width * rgba.height
        # Count meaningful (not halo) alpha to decide whether anything was found.
        solid = alpha.point(lambda a: 255 if a > ALPHA_TRIM_THRESHOLD else 0)
        opaque_pixels = sum(solid.histogram()[255:])
        coverage = opaque_pixels / total if total else 0.0

        # Trim to content. A cutout padded with transparent margin wastes CDN
        # bytes and makes the compositor's slot layout (Phase 8) fight the
        # padding instead of the garment.
        bbox = solid.getbbox()
        trimmed = rgba.crop(bbox) if bbox else rgba

        buffer = io.BytesIO()
        trimmed.save(buffer, format="PNG", optimize=True)
        png = buffer.getvalue()
        width, height = trimmed.size

    return MatteResult(
        cutout_png=png,
        alpha_coverage=coverage,
        width=width,
        height=height,
        model=MODEL_NAME,
    )
