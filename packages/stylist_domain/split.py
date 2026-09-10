"""Turning segmentation masks into garments (Step 3.2 policy).

Pure decision logic over mask metadata — no image data, no I/O. The ml service
reports what the model saw; this decides what counts as a garment. Keeping the
two apart is what lets these rules be tested exhaustively without a 112MB model
on disk, and lets the rules change without redeploying inference.

FOUR RULES, EACH EARNING ITS PLACE
----------------------------------
1. Drop masks under 2% of frame. Real output from a real photo included a
   0.41% "Dress" and a 0.00% "Bag" on a figure wearing neither. Without a
   floor, every mirror selfie invents garments.
2. Drop IoU-duplicate masks. Two classes claiming the same pixels means the
   model is unsure between them, not that there are two garments there.
3. Merge Left-shoe and Right-shoe into ONE `feet` garment. A pair of shoes is
   one thing a person owns, wears and re-pairs. taxonomy.yaml says to merge at
   the dedupe stage rather than emitting two.
4. Route drape ambiguity to review instead of guessing. ATR has never seen a
   saree, and taxonomy.yaml's atr_known_gaps predicts it fires as an
   unpredictable mix of Skirt + Dress + Scarf. Emitting three wrong garments
   from one saree is worse than asking, because the user then has to find and
   delete two phantoms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# A mask smaller than this is noise, not a garment.
MIN_AREA_PCT = 0.02

# Two masks overlapping more than this are the same region claimed twice.
IOU_DUPLICATE_THRESHOLD = 0.5

# taxonomy.yaml atr_known_gaps: "if 2+ of {Skirt, Dress, Scarf} overlap >30%
# IoU on a single subject, route to NEEDS_REVIEW".
DRAPE_AMBIGUITY_IOU = 0.30
DRAPE_AMBIGUOUS_CLASSES = frozenset({"Skirt", "Dress", "Scarf"})

# Above this share of skin the photo is worn rather than a flat-lay. Used only
# to annotate the result — nothing branches on it yet, but Phase 4's VLM prompt
# and the correction UI both want to know.
WORN_SKIN_THRESHOLD = 0.03


class SplitOutcome(StrEnum):
    OK = "ok"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True, slots=True)
class MaskInfo:
    """What the split needs to know about one mask. Deliberately not the pixels."""

    atr_label: str
    slot_hint: str | None
    area_pct: float
    bbox: tuple[int, int, int, int]
    # Index back into the caller's mask list, so it can fetch the PNG for the
    # masks that survive without this module ever touching image data.
    index: int


@dataclass(frozen=True, slots=True)
class GarmentCandidate:
    slot_hint: str | None
    atr_labels: tuple[str, ...]  # >1 when masks were merged (a pair of shoes)
    area_pct: float
    bbox: tuple[int, int, int, int]
    mask_indices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SplitResult:
    outcome: SplitOutcome
    candidates: tuple[GarmentCandidate, ...]
    reason: str | None = None
    dropped: tuple[str, ...] = field(default_factory=tuple)
    is_worn: bool = False


def iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Intersection over union of two bounding boxes.

    Boxes, not pixel masks. A pixel-exact IoU would be more accurate and needs
    the mask arrays here, which would drag image data into pure decision logic.
    For the questions being asked — "are these two classes describing the same
    region?" — box overlap is sufficient and an order of magnitude cheaper.
    """
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    intersection = (ix2 - ix1) * (iy2 - iy1)
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def _union_box(
    boxes: list[tuple[int, int, int, int]],
) -> tuple[int, int, int, int]:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def detect_drape_ambiguity(masks: list[MaskInfo]) -> str | None:
    """The saree case. Returns a reason string when the split is untrustworthy.

    A saree drapes over the lower body AND across the torso, so ATR — trained
    on Western garments — sees some unstable combination of Skirt, Dress and
    Scarf covering the same area. Two of those classes overlapping heavily is
    the signature.
    """
    ambiguous = [m for m in masks if m.atr_label in DRAPE_AMBIGUOUS_CLASSES]
    for i, first in enumerate(ambiguous):
        for second in ambiguous[i + 1 :]:
            overlap = iou(first.bbox, second.bbox)
            if overlap > DRAPE_AMBIGUITY_IOU:
                return (
                    f"{first.atr_label} and {second.atr_label} overlap "
                    f"{overlap:.0%} — likely one draped garment (saree, lehenga "
                    f"or dupatta) that ATR cannot represent; needs a manual crop"
                )
    return None


def split_masks(masks: list[MaskInfo], *, skin_pct: float = 0.0) -> SplitResult:
    """Decide which masks become garments.

    Order matters: the area floor runs FIRST so that noise masks cannot
    trigger the drape-ambiguity rule. A 0.4% phantom "Dress" overlapping a real
    skirt would otherwise send a perfectly ordinary outfit to manual review.
    """
    is_worn = skin_pct >= WORN_SKIN_THRESHOLD
    dropped: list[str] = []

    kept = []
    for mask in masks:
        if mask.area_pct < MIN_AREA_PCT:
            dropped.append(f"{mask.atr_label} ({mask.area_pct:.2%} < {MIN_AREA_PCT:.0%})")
            continue
        if mask.slot_hint is None:
            # A body part, or a class with no slot in the taxonomy. Not a
            # garment by definition.
            dropped.append(f"{mask.atr_label} (no slot mapping)")
            continue
        kept.append(mask)

    if not kept:
        return SplitResult(
            outcome=SplitOutcome.NEEDS_REVIEW,
            candidates=(),
            reason="no garment masks above the area threshold; needs a manual crop",
            dropped=tuple(dropped),
            is_worn=is_worn,
        )

    ambiguity = detect_drape_ambiguity(kept)
    if ambiguity is not None:
        return SplitResult(
            outcome=SplitOutcome.NEEDS_REVIEW,
            candidates=(),
            reason=ambiguity,
            dropped=tuple(dropped),
            is_worn=is_worn,
        )

    # Largest first, so an IoU duplicate is dropped in favour of the bigger
    # (more confident) mask rather than whichever happened to come first.
    kept.sort(key=lambda m: -m.area_pct)
    survivors: list[MaskInfo] = []
    for mask in kept:
        duplicate_of = next(
            (s for s in survivors if iou(mask.bbox, s.bbox) > IOU_DUPLICATE_THRESHOLD),
            None,
        )
        if duplicate_of is not None:
            dropped.append(f"{mask.atr_label} (IoU duplicate of {duplicate_of.atr_label})")
            continue
        survivors.append(mask)

    return SplitResult(
        outcome=SplitOutcome.OK,
        candidates=_merge_pairs(survivors),
        dropped=tuple(dropped),
        is_worn=is_worn,
    )


def _merge_pairs(masks: list[MaskInfo]) -> tuple[GarmentCandidate, ...]:
    """Collapse Left-shoe + Right-shoe into one `feet` garment.

    Shoes are the only ATR classes that describe one owned object as two
    regions. Merging them here rather than leaving two rows means the wardrobe
    shows "one pair of trainers", which is what the user owns and re-pairs.
    """
    shoes = [m for m in masks if m.atr_label in ("Left-shoe", "Right-shoe")]
    others = [m for m in masks if m.atr_label not in ("Left-shoe", "Right-shoe")]

    candidates = [
        GarmentCandidate(
            slot_hint=m.slot_hint,
            atr_labels=(m.atr_label,),
            area_pct=m.area_pct,
            bbox=m.bbox,
            mask_indices=(m.index,),
        )
        for m in others
    ]

    if shoes:
        candidates.append(
            GarmentCandidate(
                slot_hint=shoes[0].slot_hint,
                atr_labels=tuple(sorted(m.atr_label for m in shoes)),
                area_pct=sum(m.area_pct for m in shoes),
                bbox=_union_box([m.bbox for m in shoes]),
                mask_indices=tuple(sorted(m.index for m in shoes)),
            )
        )

    # Biggest garment first. The largest mask keeps the job's original
    # garment_id so a single-garment photo behaves exactly as it did in Phase 2.
    candidates.sort(key=lambda c: -c.area_pct)
    return tuple(candidates)
