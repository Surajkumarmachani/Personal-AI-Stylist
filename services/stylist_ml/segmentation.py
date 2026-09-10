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

import logging
from dataclasses import dataclass

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
