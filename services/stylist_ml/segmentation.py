"""SegFormer-B2 semantic segmentation over the 18 ATR classes.

Returns one mask per detected GARMENT class. Body parts (Background, Hair,
Face, arms, legs) are excluded from the garment list but their coverage is
reported, because "how much skin is in frame" is the cheapest available signal
for whether a photo is a flat-lay or worn — which is exactly the decision the
split logic in Step 3.2 has to make.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
No area-threshold filtering, no IoU de-duplication, no merging Left-shoe with
Right-shoe, no routing to NEEDS_REVIEW. Those are Step 3.2 policy, and they
depend on taxonomy rules and job state that a stateless inference service has
no business knowing. This reports what the model saw, with enough metadata
(area, bbox) for the caller to apply policy. The service describes; the
pipeline decides.
"""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image

from stylist_ml.preprocess import bbox_of, mask_to_png, to_tensor
from stylist_ml.registry import ATR_LABELS
from stylist_ml.runtime import LoadedModel

logger = logging.getLogger(__name__)

# ATR classes that are the person, not their clothes.
NON_GARMENT_CLASSES: frozenset[int] = frozenset(
    {
        0,  # Background
        2,  # Hair
        11,  # Face
        12,  # Left-leg
        13,  # Right-leg
        14,  # Left-arm
        15,  # Right-arm
    }
)
SKIN_CLASSES: frozenset[int] = frozenset({11, 12, 13, 14, 15})


@dataclass(frozen=True, slots=True)
class Mask:
    atr_class: int
    atr_label: str
    area_pct: float  # fraction of the FULL frame, 0..1
    bbox: tuple[int, int, int, int]
    mask_png: bytes


@dataclass(frozen=True, slots=True)
class SegmentResult:
    masks: tuple[Mask, ...]
    width: int
    height: int
    # Coverage of every non-garment class, keyed by label. Free to compute and
    # load-bearing for the Step 3.2 flat-lay/worn decision.
    non_garment_coverage: dict[str, float]
    skin_pct: float
    model: str


def segment(model: LoadedModel, image_bytes: bytes) -> SegmentResult:
    from stylist_ml.preprocess import load_rgb

    img = load_rgb(image_bytes)
    original_w, original_h = img.size

    assert model.spec.preprocess is not None
    tensor = to_tensor(img, model.spec.preprocess)

    # SegFormer emits logits at 1/4 of the input resolution — [1, 18, 128, 128]
    # for a 512x512 input. That is the architecture, not a bug.
    logits = model.session.run(None, {model.input_name: tensor})[0]

    # Upsample logits to the ORIGINAL frame before argmax, not after.
    #
    # Argmax first would give a 128x128 label map that then has to be resized,
    # and resizing a label map interpolates between class IDs — halfway between
    # class 4 and class 6 is class 5, which is a different garment entirely.
    # Interpolating logits and then taking argmax is the only correct order.
    upsampled = _bilinear_upsample(logits[0], original_h, original_w)
    labels = np.argmax(upsampled, axis=0).astype(np.uint8)

    total_px = original_h * original_w
    present, counts = np.unique(labels, return_counts=True)
    coverage = {int(c): int(n) / total_px for c, n in zip(present, counts, strict=True)}

    masks: list[Mask] = []
    non_garment: dict[str, float] = {}
    for class_id, pct in sorted(coverage.items(), key=lambda kv: -kv[1]):
        label = ATR_LABELS[class_id] if class_id < len(ATR_LABELS) else f"class_{class_id}"
        if class_id in NON_GARMENT_CLASSES:
            non_garment[label] = round(pct, 5)
            continue
        binary = labels == class_id
        box = bbox_of(binary)
        if box is None:  # pragma: no cover - unique() said it is present
            continue
        masks.append(
            Mask(
                atr_class=class_id,
                atr_label=label,
                area_pct=round(pct, 5),
                bbox=box,
                mask_png=mask_to_png(binary),
            )
        )

    skin_pct = round(sum(coverage.get(c, 0.0) for c in SKIN_CLASSES), 5)
    logger.info(
        "segmented %dx%d -> %d garment masks (%s), skin=%.1f%%",
        original_w,
        original_h,
        len(masks),
        ", ".join(f"{m.atr_label}:{m.area_pct:.1%}" for m in masks) or "none",
        skin_pct * 100,
    )
    return SegmentResult(
        masks=tuple(masks),
        width=original_w,
        height=original_h,
        non_garment_coverage=non_garment,
        skin_pct=skin_pct,
        model=model.spec.name,
    )


def _bilinear_upsample(logits: np.ndarray, height: int, width: int) -> np.ndarray:
    """Resize [C, h, w] logits to [C, height, width].

    Done with PIL per channel rather than pulling in scipy or torch: 18
    channels of bilinear resize is cheap, and the alternative is a heavyweight
    dependency in a service whose whole point is to stay small.
    """
    channels = logits.shape[0]
    out = np.empty((channels, height, width), dtype=np.float32)
    for c in range(channels):
        # Float32 PIL images ("F" mode) resize without quantising, which
        # matters: logits are unbounded and clipping them to 0..255 would
        # change the argmax.
        plane = Image.fromarray(logits[c].astype(np.float32), mode="F")
        out[c] = np.asarray(
            plane.resize((width, height), resample=Image.Resampling.BILINEAR),
            dtype=np.float32,
        )
    return out


# ---------------------------------------------------------------- flat-lay split

# A component smaller than this share of the frame is noise — a shadow edge, a
# label, a fold that matted separately — not a garment.
MIN_COMPONENT_AREA_PCT = 0.02

# Above this many components the union has FRAGMENTED rather than resolved into
# garments, and the caller is better off with one whole-frame candidate than
# with eleven phantom ones. A real flat-lay photo holds a handful of items.
MAX_COMPONENTS = 8

# A component this far inside a LARGER component's box is part of that garment,
# not a garment of its own.
#
# THE CASE THIS EXISTS FOR is a print, a graphic panel or zari work that mattes
# as its own blob because it contrasts with the fabric around it. Measured
# 2026-09-17: a shirt with a photographic panel printed on the chest produced a
# second component sitting inside the first. Emitting it would put a phantom
# garment in the wardrobe whose "cutout" is somebody's chest print — and this
# taxonomy has `embroidered`, `zari_work`, `block_print` and `sequinned` in it,
# so contrasting panels are the norm here rather than an edge case.
CONTAINED_THRESHOLD = 0.80


def flatlay_components(masks: list[Any], width: int, height: int) -> list[dict[str, Any]]:
    """Spatially disjoint garments in a flat-lay, from the masks' PIXELS.

    WHY THIS WORKS WHEN THE LABELS DO NOT
    --------------------------------------
    On a flat-lay, SegFormer is a human-parsing model with no human to parse,
    so its CLASS LABELS are shape guesses — four folded garments produced
    `Upper-clothes`, `Pants`, `Skirt`, `Bag` and `Hat`. Rule 5 in
    stylist_domain.split therefore discards those labels, and until now it
    discarded the masks with them, collapsing the photo to ONE garment.

    But the labels being wrong does not make the PIXELS wrong. Measured
    2026-09-17 on a real photograph of four folded garments: the union of those
    five mislabelled masks has exactly FOUR connected components, and their
    bounding boxes match the four garments' positions. The model saw the
    fabric correctly and only named it badly.

    So: union every mask, label connected components, and return one per
    component. No extra inference and no extra model — this is arithmetic on an
    array the segment pass already produced.

    ONE MASK CAN SPAN SEVERAL COMPONENTS, which is why the component and not
    the mask is the unit here. In that measured photo `Upper-clothes` covered
    191,798 pixels across three separate garments; grouping by mask would have
    merged them back together.

    THE FALSE POSITIVE TO WATCH is a single garment that mattes into two
    blobs — a sleeve photographed clear of the body, a belt loop. That becomes
    two garments, one of them a phantom. The area floor removes the small
    cases; the rest is why the correction UI exists.
    """
    from scipy import ndimage

    if not masks:
        return []

    union: Any = None
    for m in masks:
        arr = _mask_to_bool(m, width, height)
        if arr is None:
            continue
        union = arr if union is None else (union | arr)
    if union is None or not union.any():
        return []

    labelled, count = ndimage.label(union)
    if count == 0:
        return []

    frame = float(width * height) or 1.0
    found: list[dict[str, Any]] = []
    for cid in range(1, count + 1):
        blob = labelled == cid
        area = int(blob.sum())
        if area / frame < MIN_COMPONENT_AREA_PCT:
            continue
        ys, xs = np.where(blob)
        found.append(
            {
                "bbox": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())],
                "area_pct": area / frame,
                # The component's own mask, not any input mask's. This is what
                # the matte crops to, so it has to be the blob rather than the
                # mislabelled region that contributed to it.
                "mask_png_b64": _bool_to_png_b64(blob),
            }
        )

    # Largest first, so a caller that takes only the primary takes the biggest.
    found.sort(key=lambda c: -float(c["area_pct"]))
    found = _drop_contained(found)
    if len(found) > MAX_COMPONENTS:
        return []
    return found


def _box_containment(inner: list[int], outer: list[int]) -> float:
    """Share of `inner`'s box that lies inside `outer`'s."""
    ix1, iy1 = max(inner[0], outer[0]), max(inner[1], outer[1])
    ix2, iy2 = min(inner[2], outer[2]), min(inner[3], outer[3])
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inner_area = (inner[2] - inner[0]) * (inner[3] - inner[1])
    return ((ix2 - ix1) * (iy2 - iy1)) / inner_area if inner_area > 0 else 0.0


def _drop_contained(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove components that sit inside a bigger one.

    `found` is already sorted largest-first, so every candidate is only ever
    tested against components that survived AND are at least as large — which
    means a print inside a shirt is dropped, and two shirts lying side by side
    (overlapping boxes, neither contained) both survive.

    The dropped blob's pixels are not lost: the caller crops to the containing
    component's BOUNDING BOX, which already covers them.
    """
    kept: list[dict[str, Any]] = []
    for candidate in found:
        inner = [int(v) for v in candidate["bbox"]]
        if any(
            _box_containment(inner, [int(v) for v in k["bbox"]]) >= CONTAINED_THRESHOLD
            for k in kept
        ):
            continue
        kept.append(candidate)
    return kept


def _mask_to_bool(mask: Any, width: int, height: int) -> Any:
    """One mask's PNG as a boolean array, or None if it cannot be read."""
    try:
        img = Image.open(io.BytesIO(mask.mask_png)).convert("L")
    except Exception:  # pragma: no cover - a mask we just produced
        return None
    if img.size != (width, height):
        img = img.resize((width, height), resample=Image.Resampling.NEAREST)
    return np.array(img) > 127


def _bool_to_png_b64(blob: Any) -> str:
    buf = io.BytesIO()
    Image.fromarray((blob.astype(np.uint8) * 255), mode="L").save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")
