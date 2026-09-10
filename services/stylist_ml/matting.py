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

import hashlib
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
_sha256: str | None = None


def loaded_sha256() -> str | None:
    """Digest of the weights this process actually loaded.

    Computed at warm time so /models reports u2net on the same terms as the
    models that go through runtime.load() — an inventory where one entry has no
    checksum is an inventory you cannot use to answer "which weights served
    that request".
    """
    return _sha256


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
    global _loaded, _sha256
    try:
        _session()
        path = model_path()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()

        from stylist_ml.registry import U2NET

        if U2NET.sha256 and digest != U2NET.sha256:
            raise RuntimeError(
                f"{path} checksum mismatch: registry pins {U2NET.sha256[:16]}…, "
                f"file is {digest[:16]}…. Refusing to serve unverified weights."
            )
        _sha256 = digest
        _loaded = True
        logger.info("matting model warm: %s sha256=%s…", path, digest[:16])
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


def _crop_to_mask(image_bytes: bytes, mask_png: bytes) -> tuple[bytes, Image.Image]:
    """Crop the image and its mask to the mask's bbox, WITHOUT masking pixels.

    u2net must see real image content inside the crop. Blanking everything
    outside the mask to white first — which is what this used to do — hands the
    model a coloured shape on a white field and it regularly decides the WHITE
    is the subject. Observed directly: a maroon shirt came back as a cutout
    whose opaque pixels were white, so colour extraction read `white` for a
    maroon garment.

    Cropping still matters on its own: handing u2net a 4000x3000 frame with one
    shirt in a corner spends the model's fixed input resolution on empty pixels.
    """
    with Image.open(io.BytesIO(image_bytes)) as base, Image.open(io.BytesIO(mask_png)) as mask:
        rgb = base.convert("RGB")
        binary = mask.convert("L")
        if binary.size != rgb.size:
            # The mask comes from /segment at full frame size; a mismatch means
            # the caller paired a mask with the wrong image.
            binary = binary.resize(rgb.size, resample=Image.Resampling.NEAREST)

        box = binary.getbbox()
        if box is not None:
            rgb = rgb.crop(box)
            binary = binary.crop(box)

        buffer = io.BytesIO()
        rgb.save(buffer, format="PNG", optimize=False)
        return buffer.getvalue(), binary


def matte(image_bytes: bytes, mask_png: bytes | None = None) -> MatteResult:
    """Remove the background and trim to the subject's bounding box.

    `mask_png` restricts matting to one garment out of a multi-garment frame
    (Step 3.2 passes a mask from /segment). The mask is used to CROP the input
    and then to intersect the resulting alpha — not to blank pixels before
    inference. See _crop_to_mask for why: pre-masking made u2net treat the
    white fill as the subject and produced white cutouts of coloured garments.
    """
    from rembg import remove

    session = _session()

    mask_crop: Image.Image | None = None
    if mask_png is not None:
        image_bytes, mask_crop = _crop_to_mask(image_bytes, mask_png)

    cutout = remove(image_bytes, session=session)

    with Image.open(io.BytesIO(cutout)) as img:
        rgba = img.convert("RGBA")

        if mask_crop is not None:
            # INTERSECT the two alphas rather than trusting either alone.
            #
            # The segmentation mask knows WHICH garment was asked about but has
            # blocky edges (it is a 512x512 label map upsampled to frame size).
            # u2net produces a fine, feathered alpha but does not know which
            # garment we meant. Multiplying keeps u2net's edge quality inside
            # the region segmentation chose, so a mirror selfie's shirt comes
            # back as the shirt, cleanly cut.
            import numpy as np

            fine = np.asarray(rgba.getchannel("A"), dtype=np.float32) / 255.0
            coarse = np.asarray(mask_crop.resize(rgba.size), dtype=np.float32) / 255.0
            product = fine * coarse

            # DEGRADE TO THE MASK WHEN u2net THREW THE GARMENT AWAY.
            #
            # u2net is unreliable on a tight crop with little surrounding
            # context. Measured on one: its alpha summed to 17 against the
            # segmentation mask's 48,793 over the same 169x313 region — it
            # found essentially nothing. Intersecting with that loses a garment
            # segmentation had located correctly, and the whole photo routes to
            # manual review.
            #
            # The mask is already a usable alpha: blockier edges, right region.
            # So u2net is an ENHANCEMENT — kept when it agrees with
            # segmentation, discarded when it contradicts it. Every stage has a
            # fallback (§C6); this is matting's.
            coarse_area = float(coarse.sum())
            if coarse_area > 0 and float(product.sum()) < 0.5 * coarse_area:
                logger.info(
                    "u2net alpha covered %.1f%% of the segmentation mask; "
                    "falling back to the mask as alpha",
                    100 * float(product.sum()) / coarse_area,
                )
                product = coarse

            # REBUILD RGB FROM THE ORIGINAL CROP, not from u2net's output.
            #
            # rembg zeroes the colour channels wherever its own alpha is 0, so
            # reusing its RGB and then widening the alpha reveals BLACK pixels
            # rather than the garment. Measured: a maroon top and denim
            # trousers both came back with primary_colour `black`, because the
            # pixels the mask exposed had already been blanked.
            #
            # Alpha and colour therefore come from different places on purpose:
            # the alpha is whatever we decided above, the colour is always the
            # untouched original.
            with Image.open(io.BytesIO(image_bytes)) as original:
                rgb = original.convert("RGB")
            rgba = rgb.copy().convert("RGBA")
            rgba.putalpha(Image.fromarray((product * 255).astype("uint8"), mode="L"))

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
