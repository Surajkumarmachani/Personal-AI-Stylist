"""Image preprocessing shared by the ONNX models.

Every model here wants the same shape of work — resize, rescale to [0,1],
normalise by mean/std, transpose to NCHW float32 — with different constants.
Those constants come from each model's registry entry rather than being written
at the call site, because getting them wrong does not raise: it silently
degrades accuracy, which is the hardest kind of bug to notice and the easiest
to introduce.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from stylist_ml.registry import Preprocess


def load_rgb(image_bytes: bytes) -> Image.Image:
    """Decode to RGB, dropping any alpha.

    Alpha has to go before normalisation: a cutout's transparent background
    decodes as RGBA, and feeding a 4-channel array to a 3-channel model is a
    shape error at best and garbage at worst. Compositing onto white rather
    than black because a black fill reads as a dark garment edge to a model
    trained on photographs.
    """
    opened = Image.open(io.BytesIO(image_bytes))
    img: Image.Image = opened
    if opened.mode in ("RGBA", "LA", "P"):
        rgba = opened.convert("RGBA")
        white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        img = Image.alpha_composite(white, rgba)
    return img.convert("RGB")


def to_tensor(img: Image.Image, spec: Preprocess) -> np.ndarray:
    """PIL image -> NCHW float32 batch of one, normalised per `spec`.

    Note the resize is UNCONDITIONAL and does not preserve aspect ratio, which
    matches how both of these models were trained and evaluated. Letterboxing
    instead would be "more correct" in the abstract and measurably worse in
    practice, because it is not the distribution the weights saw.
    """
    height, width = spec.size
    resized = img.resize((width, height), resample=spec.resample)

    array = np.asarray(resized, dtype=np.float32) / 255.0  # HWC, [0,1]
    mean = np.asarray(spec.mean, dtype=np.float32)
    std = np.asarray(spec.std, dtype=np.float32)
    array = (array - mean) / std

    # HWC -> CHW -> NCHW. ascontiguousarray because onnxruntime copies a
    # non-contiguous buffer anyway; doing it once here is cheaper than letting
    # it happen per inference.
    return np.ascontiguousarray(array.transpose(2, 0, 1)[np.newaxis, ...], dtype=np.float32)


def mask_to_png(mask: np.ndarray) -> bytes:
    """Boolean mask -> 1-bit PNG.

    Mode "1" rather than "L": a segmentation mask carries one bit per pixel and
    storing it as 8-bit greyscale is 8x the bytes for no information. At a
    handful of masks per photo over a 60-photo onboarding burst that difference
    is real.
    """
    img = Image.fromarray((mask.astype(np.uint8) * 255), mode="L").convert("1")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def bbox_of(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    """Tight [x1, y1, x2, y2] around the True pixels, or None if empty.

    Computed with any() along each axis rather than PIL's getbbox() so it works
    on the raw boolean array without a round trip through an image object —
    this runs once per detected class per photo.
    """
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    if not rows.any() or not cols.any():
        return None
    y1, y2 = np.where(rows)[0][[0, -1]]
    x1, x2 = np.where(cols)[0][[0, -1]]
    return int(x1), int(y1), int(x2) + 1, int(y2) + 1
