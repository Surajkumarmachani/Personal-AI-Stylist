"""Split policy: which masks become garments (Step 3.2).

Pure logic, so these run without a model, a database or a container. That is
the payoff of keeping policy out of the inference service: the rules that decide
what a user sees in their wardrobe are exhaustively testable in milliseconds.

Every threshold here traces to something observed or specified, and each test
says which.
"""

from __future__ import annotations

import pytest

from stylist_domain.split import (
    DRAPE_AMBIGUITY_IOU,
    MIN_AREA_PCT,
    MaskInfo,
    SplitOutcome,
    detect_drape_ambiguity,
    iou,
    split_masks,
)


def mask(
    label: str,
    slot: str | None,
    area: float,
    bbox: tuple[int, int, int, int],
    index: int = 0,
) -> MaskInfo:
    return MaskInfo(atr_label=label, slot_hint=slot, area_pct=area, bbox=bbox, index=index)


# ------------------------------------------------------------------- iou


def test_iou_of_identical_boxes_is_one() -> None:
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0


def test_iou_of_disjoint_boxes_is_zero() -> None:
    assert iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0


def test_iou_of_touching_boxes_is_zero() -> None:
    """Edges that meet do not overlap. Counting them would make a shirt and the
    trousers directly below it look like the same garment."""
    assert iou((0, 0, 10, 10), (10, 0, 20, 10)) == 0.0


def test_iou_is_partial_for_partial_overlap() -> None:
    # 10x10 and 10x10 sharing a 5x10 strip: 50 / (100 + 100 - 50).
    assert iou((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(50 / 150)


# ------------------------------------------------------- the area floor


def test_noise_masks_below_the_area_floor_are_dropped() -> None:
    """The exact output a real photo produced.

    A synthetic figure wearing a top and trousers came back with a 0.41%
    "Dress" and a 0.00% "Bag". Without a floor, every mirror selfie invents
    garments the user then has to find and delete.
    """
    result = split_masks(
        [
            mask("Upper-clothes", "upper_base", 0.0915, (216, 157, 385, 471), 0),
            mask("Pants", "lower", 0.0461, (242, 469, 360, 758), 1),
            mask("Dress", "full_body", 0.0041, (277, 229, 331, 373), 2),
            mask("Bag", "bag", 0.00002, (233, 467, 235, 468), 3),
        ]
    )
    assert result.outcome is SplitOutcome.OK
    labels = {label for c in result.candidates for label in c.atr_labels}
    assert labels == {"Upper-clothes", "Pants"}
    assert any("Dress" in d for d in result.dropped)
    assert any("Bag" in d for d in result.dropped)


def test_a_mask_exactly_at_the_floor_is_kept() -> None:
    result = split_masks([mask("Upper-clothes", "upper_base", MIN_AREA_PCT, (0, 0, 50, 50))])
    assert result.outcome is SplitOutcome.OK
    assert len(result.candidates) == 1


def test_body_parts_are_never_garments() -> None:
    """A slot_hint of None means the taxonomy maps this class to no slot —
    someone's arm is not something they own."""
    result = split_masks(
        [
            mask("Upper-clothes", "upper_base", 0.20, (0, 0, 100, 100), 0),
            mask("Left-arm", None, 0.10, (0, 0, 40, 90), 1),
            mask("Face", None, 0.05, (30, 0, 70, 30), 2),
        ]
    )
    assert len(result.candidates) == 1
    assert result.candidates[0].atr_labels == ("Upper-clothes",)


# -------------------------------------------------------- shoe merging


def test_left_and_right_shoes_become_one_garment() -> None:
    """A pair of shoes is one thing a person owns, wears and re-pairs.

    taxonomy.yaml: "merge L/R into ONE garment at the dedupe stage, not two".
    """
    result = split_masks(
        [
            mask("Upper-clothes", "upper_base", 0.20, (200, 100, 400, 400), 0),
            mask("Left-shoe", "feet", 0.03, (300, 760, 370, 800), 1),
            mask("Right-shoe", "feet", 0.03, (230, 760, 300, 800), 2),
        ]
    )
    feet = [c for c in result.candidates if c.slot_hint == "feet"]
    assert len(feet) == 1, "shoes were not merged into a single garment"
    assert feet[0].atr_labels == ("Left-shoe", "Right-shoe")
    assert feet[0].area_pct == pytest.approx(0.06)
    # The bbox must span both shoes, or a later crop would cut one off.
    assert feet[0].bbox == (230, 760, 370, 800)
    assert feet[0].mask_indices == (1, 2)


def test_a_single_shoe_still_becomes_a_garment() -> None:
    """Only one shoe visible is common in a worn photo; it is still a pair the
    person owns."""
    result = split_masks(
        [
            mask("Upper-clothes", "upper_base", 0.20, (200, 100, 400, 400), 0),
            mask("Left-shoe", "feet", 0.03, (300, 760, 370, 800), 1),
        ]
    )
    feet = [c for c in result.candidates if c.slot_hint == "feet"]
    assert len(feet) == 1
    assert feet[0].atr_labels == ("Left-shoe",)


# ----------------------------------------------------------- IoU dedupe


def test_overlapping_classes_are_deduplicated_keeping_the_larger() -> None:
    """Two classes claiming the same pixels means the model is unsure between
    them, not that there are two garments there."""
    result = split_masks(
        [
            mask("Upper-clothes", "upper_base", 0.25, (100, 100, 300, 400), 0),
            mask("Dress", "full_body", 0.20, (105, 105, 295, 395), 1),
        ]
    )
    assert len(result.candidates) == 1
    assert result.candidates[0].atr_labels == ("Upper-clothes",)
    assert any("IoU duplicate" in d for d in result.dropped)


def test_adjacent_garments_are_not_deduplicated() -> None:
    """A top above trousers is the normal case and must survive.

    If the dedupe were too aggressive here, every worn photo would collapse to
    one garment — which looks like segmentation failing rather than policy
    misfiring.
    """
    result = split_masks(
        [
            mask("Upper-clothes", "upper_base", 0.15, (200, 150, 400, 470), 0),
            mask("Pants", "lower", 0.12, (240, 470, 360, 760), 1),
        ]
    )
    assert len(result.candidates) == 2


# --------------------------------------------------- drape ambiguity


def test_a_saree_style_overlap_goes_to_review_not_three_garments() -> None:
    """THE ETHNIC-WEAR CASE, and the reason this rule exists.

    ATR has never seen a saree. taxonomy.yaml's atr_known_gaps predicts it
    fires as an unpredictable mix of Skirt + Dress + Scarf over the same
    region. Emitting three garments from one saree is worse than asking,
    because the user has to hunt down and delete two phantoms — and would
    reasonably conclude the product does not understand their clothes.
    """
    result = split_masks(
        [
            mask("Dress", "full_body", 0.35, (150, 200, 450, 850), 0),
            mask("Skirt", "lower", 0.28, (160, 400, 440, 860), 1),
            mask("Scarf", "drape", 0.15, (200, 180, 420, 500), 2),
        ]
    )
    assert result.outcome is SplitOutcome.NEEDS_REVIEW
    assert result.candidates == ()
    assert result.reason is not None
    assert "manual crop" in result.reason


def test_noise_cannot_trigger_the_drape_rule() -> None:
    """Ordering matters: the area floor runs BEFORE the ambiguity check.

    A 0.4% phantom "Dress" overlapping a real skirt would otherwise send a
    perfectly ordinary outfit to manual review — the filter creating the
    problem it exists to prevent.
    """
    result = split_masks(
        [
            mask("Skirt", "lower", 0.30, (150, 400, 450, 860), 0),
            mask("Dress", "full_body", 0.004, (200, 420, 300, 600), 1),
        ]
    )
    assert result.outcome is SplitOutcome.OK
    assert len(result.candidates) == 1
    assert result.candidates[0].atr_labels == ("Skirt",)


def test_a_skirt_and_a_separate_top_are_not_drape_ambiguity() -> None:
    """Skirt and Dress are both in the ambiguous set, but non-overlapping
    regions are two garments, not one confused drape."""
    assert (
        detect_drape_ambiguity(
            [
                mask("Skirt", "lower", 0.20, (150, 500, 450, 860), 0),
                mask("Dress", "full_body", 0.05, (150, 100, 450, 200), 1),
            ]
        )
        is None
    )


def test_drape_threshold_matches_the_taxonomys_documented_value() -> None:
    """taxonomy.yaml atr_known_gaps specifies >30% IoU. The code and the
    frozen taxonomy must not drift apart."""
    assert DRAPE_AMBIGUITY_IOU == 0.30


# ------------------------------------------------------------ no masks


def test_zero_masks_is_review_never_a_silent_success() -> None:
    """The plan: "Zero masks -> NEEDS_REVIEW with a manual crop UI, never a
    silent failure"."""
    result = split_masks([])
    assert result.outcome is SplitOutcome.NEEDS_REVIEW
    assert result.reason is not None


def test_only_noise_masks_is_also_review() -> None:
    result = split_masks([mask("Bag", "bag", 0.0001, (0, 0, 2, 2))])
    assert result.outcome is SplitOutcome.NEEDS_REVIEW


# -------------------------------------------------------------- worn flag


def test_skin_coverage_marks_a_photo_as_worn() -> None:
    """Free signal from segmentation, and Phase 4's VLM prompt wants it: a
    garment on a person photographs differently from one on a bed."""
    masks = [mask("Upper-clothes", "upper_base", 0.20, (0, 0, 100, 100))]
    assert split_masks(masks, skin_pct=0.12).is_worn is True
    assert split_masks(masks, skin_pct=0.0).is_worn is False


def test_candidates_are_ordered_largest_first() -> None:
    """The largest garment inherits the job's existing garment_id, so a
    single-garment photo behaves exactly as it did in Phase 2."""
    result = split_masks(
        [
            mask("Left-shoe", "feet", 0.03, (300, 760, 370, 800), 0),
            mask("Upper-clothes", "upper_base", 0.20, (200, 150, 400, 470), 1),
            mask("Pants", "lower", 0.12, (240, 470, 360, 760), 2),
        ]
    )
    areas = [c.area_pct for c in result.candidates]
    assert areas == sorted(areas, reverse=True)
