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
5. DO NOT SPLIT A FLAT-LAY. See below — this is the rule that matters most in
   practice, because flat-lays are how people actually photograph clothes.

THE MODEL PARSES PEOPLE, AND MOST PHOTOS HAVE NO PERSON IN THEM
----------------------------------------------------------------
`segformer_b2_clothes` is a HUMAN PARSING model: it assigns every pixel of a
photo of a PERSON to a body-or-garment region. Give it a flat-lay — a garment
on a bed or a white background, no person — and it has nothing to parse. It
does not decline; it carves the image into regions and labels each with
whichever ATR class the shape resembles.

Measured on three flat-lay photos of jeans:

    photo A -> "Upper-clothes" + "Bag"   (2 garments from one pair of jeans)
    photo B -> "Dress"                   (slot full_body)
    photo C -> "Upper-clothes"           (slot upper_base)

Not one produced `Pants`. The masks were also fragmentary — the cutouts came
out punched full of holes, because the model was splitting one garment across
regions it was not confident about.

So when no person is detected, the whole photo is ONE garment and the mask is
the matte, not a parse. That is both more accurate and strictly cheaper. The
`skin_pct` signal this needs was already being computed and reported; nothing
branched on it until now.

The cost of being wrong is asymmetric, which sets the direction of the
threshold: treating a worn photo as a flat-lay yields one garment the user can
correct, while treating a flat-lay as worn yields several phantom garments they
must find and delete — and a torn cutout that looks like a broken product.
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

# Above this share of skin the photo is WORN rather than a flat-lay.
#
# THIS IS LOAD-BEARING. Rule 5 below branches on it: a photo judged flat-lay is
# not split at all. The comment here used to say "nothing branches on it yet",
# which was true when the constant only annotated the result — and it stayed
# there after rule 5 made it control flow, so its calibration was never
# revisited. That staleness is why the value below was wrong for eight months
# of this project rather than for eight minutes.
#
# 0.03 WAS WRONG, AND WRONG IN A WAY SPECIFIC TO THIS WARDROBE.
# Measured 2026-09-17 against real photographs (the first real photographs this
# pipeline has ever seen — everything before was procedurally generated):
#
#   WORN, ethnic          skin_pct        WORN, western / flat-lay   skin_pct
#   saree (arms bare)       0.0213        folded flat-lay (4 items)    0.0000
#   kurta, full sleeve      0.0185        dress shirt, product shot    0.0000
#   salwar kameez           0.0123        t-shirt, product shot        0.0000
#   sherwani                0.0088        lehenga, product shot        0.0000
#
# At 0.03, ALL FOUR worn ethnic photos were misread as flat-lays — including
# the saree, where the wearer's face, shoulders and arms are plainly visible.
# The reason is structural, not a tuning accident: a kurta, sherwani or fully
# draped saree covers the wrist, the neck and the ankle. A t-shirt and jeans do
# not. A skin-fraction threshold calibrated on Western clothing encodes an
# assumption about how much of a person their clothes leave visible, and that
# assumption does not survive contact with ethnic wear.
#
# The observed separation is clean — every true flat-lay measured exactly
# 0.0000 — so 0.005 sits with margin on both sides of it.
#
# PROVISIONAL — set 2026-09-17 on n=8 PUBLIC images, not this wardrobe.
# Resolves when: the owner's real photographs are ingested and skin_pct is
# measured across them. The failure mode to watch is the opposite one: a
# flat-lay shot on a wooden floor or skin-toned surface reading as worn, which
# would split it into the garbage masks rule 5 exists to suppress.
WORN_SKIN_THRESHOLD = 0.005


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
class ComponentInfo:
    """One spatially disjoint blob in a flat-lay. NO label, deliberately.

    Like MaskInfo this is not the pixels — the connected-component pass runs in
    the ml service, where the arrays already are, and this module receives only
    the geometry. Keeping image data out of the decision logic is the same rule
    that makes `iou` work on boxes rather than masks.
    """

    bbox: tuple[int, int, int, int]
    area_pct: float
    index: int


@dataclass(frozen=True, slots=True)
class GarmentCandidate:
    slot_hint: str | None
    atr_labels: tuple[str, ...]  # >1 when masks were merged (a pair of shoes)
    area_pct: float
    bbox: tuple[int, int, int, int]
    mask_indices: tuple[int, ...]
    # Index into the ml service's `flatlay_components`, set ONLY on the
    # flat-lay path. The caller uses it to fetch that component's own mask
    # instead of unioning `mask_indices`, because on a flat-lay the masks are
    # mislabelled and one of them can span several garments.
    component_index: int | None = None


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


def split_masks(
    masks: list[MaskInfo],
    *,
    skin_pct: float = 0.0,
    components: tuple[ComponentInfo, ...] = (),
) -> SplitResult:
    """Decide which masks become garments.

    Order matters: the area floor runs FIRST so that noise masks cannot
    trigger the drape-ambiguity rule. A 0.4% phantom "Dress" overlapping a real
    skirt would otherwise send a perfectly ordinary outfit to manual review.
    """
    is_worn = skin_pct >= WORN_SKIN_THRESHOLD
    dropped: list[str] = []

    # RULE 5: a flat-lay is not PARSED — but it may still be SPLIT.
    #
    # No person in frame means the human-parsing model had nothing to parse and
    # its class labels are shape guesses, so the labels go in the bin either
    # way. The question is how many garments are in the photo.
    #
    # Originally this returned exactly one whole-frame candidate, which was
    # right about the labels and wrong about the count: a photo of four folded
    # garments catalogued as one garment. The fix is that the labels being
    # wrong does not make the PIXELS wrong — measured 2026-09-17, the union of
    # five mislabelled masks over four folded garments has exactly four
    # connected components, positioned correctly.
    #
    # So when the ml service reports disjoint components, each becomes a
    # candidate. When it reports none (an older image, an empty frame, or a
    # union that fragmented past its cap) this falls back to the single
    # whole-frame candidate, which is the previous behaviour.
    if not is_worn:
        if len(components) > 1:
            return SplitResult(
                outcome=SplitOutcome.OK,
                candidates=tuple(
                    GarmentCandidate(
                        # Still None. A component tells us WHERE a garment is,
                        # never WHAT it is — that is the VLM's job, and
                        # inventing a slot here would reintroduce exactly the
                        # error rule 5 exists to prevent.
                        slot_hint=None,
                        atr_labels=("flat_lay",),
                        area_pct=c.area_pct,
                        bbox=c.bbox,
                        mask_indices=(),
                        component_index=c.index,
                    )
                    for c in components
                ),
                reason=f"flat-lay split into {len(components)} disjoint garments",
                dropped=tuple(f"{m.atr_label} (flat-lay: label discarded)" for m in masks),
                is_worn=False,
            )
        return SplitResult(
            outcome=SplitOutcome.OK,
            candidates=(
                GarmentCandidate(
                    # None, NOT the model's guess. `upper_base` on a pair of
                    # jeans is worse than an honest unknown, because a wrong
                    # slot silently excludes the garment from every outfit that
                    # needs a `lower`.
                    slot_hint=None,
                    atr_labels=("flat_lay",),
                    area_pct=components[0].area_pct if components else 1.0,
                    bbox=components[0].bbox if components else (0, 0, 0, 0),
                    mask_indices=(),
                    component_index=components[0].index if components else None,
                ),
            ),
            dropped=tuple(f"{m.atr_label} (flat-lay: not split)" for m in masks),
            is_worn=False,
        )

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
