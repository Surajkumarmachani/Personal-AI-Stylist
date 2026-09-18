"""Split policy: which masks become garments (Step 3.2).

Pure logic, so these run without a model, a database or a container. That is
the payoff of keeping policy out of the inference service: the rules that decide
what a user sees in their wardrobe are exhaustively testable in milliseconds.

Every threshold here traces to something observed or specified, and each test
says which.
"""

from __future__ import annotations

from typing import Any

import pytest

from stylist_domain.split import (
    DRAPE_AMBIGUITY_IOU,
    MIN_AREA_PCT,
    WORN_SKIN_THRESHOLD,
    MaskInfo,
    SplitOutcome,
    detect_drape_ambiguity,
    iou,
    split_masks,
)

# Every rule below — the area floor, IoU dedupe, the shoe merge, the drape
# check — is part of the PARSE path, which only runs when a person is in the
# photo. These tests always assumed that and never said so: they called
# `split_masks` without `skin_pct`, which defaults to 0.0, i.e. a FLAT-LAY.
#
# That was harmless while nothing branched on it and became wrong the moment
# rule 5 landed. Stating the precondition is the point — a test whose
# assumption is implicit passes for a reason nobody can see.
WORN = 0.12


def split_masks_worn(masks, **kw):
    """`split_masks` for a photo with a person in it."""
    kw.setdefault("skin_pct", WORN)
    return split_masks(masks, **kw)


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
    result = split_masks_worn(
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
    result = split_masks_worn([mask("Upper-clothes", "upper_base", MIN_AREA_PCT, (0, 0, 50, 50))])
    assert result.outcome is SplitOutcome.OK
    assert len(result.candidates) == 1


def test_body_parts_are_never_garments() -> None:
    """A slot_hint of None means the taxonomy maps this class to no slot —
    someone's arm is not something they own."""
    result = split_masks_worn(
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
    result = split_masks_worn(
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
    result = split_masks_worn(
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
    result = split_masks_worn(
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
    result = split_masks_worn(
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
    result = split_masks_worn(
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
    result = split_masks_worn(
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
    result = split_masks([], skin_pct=WORN)
    assert result.outcome is SplitOutcome.NEEDS_REVIEW
    assert result.reason is not None


def test_only_noise_masks_is_also_review() -> None:
    result = split_masks_worn([mask("Bag", "bag", 0.0001, (0, 0, 2, 2))])
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
    result = split_masks_worn(
        [
            mask("Left-shoe", "feet", 0.03, (300, 760, 370, 800), 0),
            mask("Upper-clothes", "upper_base", 0.20, (200, 150, 400, 470), 1),
            mask("Pants", "lower", 0.12, (240, 470, 360, 760), 2),
        ]
    )
    areas = [c.area_pct for c in result.candidates]
    assert areas == sorted(areas, reverse=True)


# --------------------------------- rule 5: a flat-lay is one garment


def test_a_flat_lay_is_not_split_into_several_garments() -> None:
    """THE BUG THIS RULE EXISTS FOR, reproduced from real output.

    One flat-lay photo of jeans produced "Upper-clothes" AND "Bag", i.e. two
    garments from one pair of trousers — a torn-looking cutout and a phantom
    bag the user would then have to find and delete. `segformer_b2_clothes`
    parses PEOPLE; with no person in frame its class labels are shape guesses.
    """
    result = split_masks(
        [
            mask("Upper-clothes", "upper_base", 0.42, (10, 10, 400, 500)),
            mask("Bag", "bag", 0.06, (300, 380, 380, 460)),
        ],
        skin_pct=0.0,
    )

    assert result.outcome is SplitOutcome.OK
    assert len(result.candidates) == 1, "one photo, one garment"
    assert result.is_worn is False


def test_a_flat_lay_candidate_carries_no_slot_guess() -> None:
    """`upper_base` on a pair of jeans is worse than an honest unknown: a wrong
    slot silently excludes the garment from every outfit needing a `lower`, and
    nothing downstream can tell it was a guess."""
    result = split_masks(
        [mask("Dress", "full_body", 0.55, (0, 0, 500, 700))],
        skin_pct=0.0,
    )
    assert result.candidates[0].slot_hint is None
    assert result.candidates[0].atr_labels == ("flat_lay",)


def test_the_discarded_guesses_are_reported_not_hidden() -> None:
    """What the model said is still recorded. A silent discard makes "why did
    this become one garment" unanswerable from the job record."""
    result = split_masks(
        [
            mask("Upper-clothes", "upper_base", 0.42, (10, 10, 400, 500)),
            mask("Bag", "bag", 0.06, (300, 380, 380, 460)),
        ],
        skin_pct=0.0,
    )
    assert any("Upper-clothes" in d and "flat-lay" in d for d in result.dropped)
    assert any("Bag" in d for d in result.dropped)


def test_a_flat_lay_with_no_masks_at_all_is_still_one_garment() -> None:
    """The model finding nothing on a flat-lay is the EXPECTED case, not a
    failure: there is no person, so a human parser has nothing to say. The
    photo still contains a garment and matting will find its edges."""
    result = split_masks([], skin_pct=0.0)
    assert result.outcome is SplitOutcome.OK
    assert len(result.candidates) == 1


def test_a_worn_photo_is_still_parsed_normally() -> None:
    """The counterweight. Rule 5 must not disable the split for the photos the
    model was actually trained on — a mirror selfie with three garments must
    still produce three."""
    result = split_masks(
        [
            mask("Upper-clothes", "upper_base", 0.20, (100, 50, 300, 250)),
            mask("Pants", "lower", 0.25, (100, 250, 300, 500)),
            mask("Left-shoe", "feet", 0.04, (100, 500, 180, 560)),
        ],
        skin_pct=WORN,
    )
    assert result.outcome is SplitOutcome.OK
    assert len(result.candidates) == 3
    assert result.is_worn is True


def test_the_threshold_errs_toward_flat_lay() -> None:
    """The costs are asymmetric. Treating a worn photo as a flat-lay gives one
    garment the user can correct; treating a flat-lay as worn gives several
    phantoms to hunt down and a torn cutout that reads as a broken product."""
    masks = [
        mask("Upper-clothes", "upper_base", 0.42, (10, 10, 400, 500)),
        mask("Bag", "bag", 0.06, (300, 380, 380, 460)),
    ]
    just_below = split_masks(masks, skin_pct=WORN_SKIN_THRESHOLD - 0.001)
    at_threshold = split_masks(masks, skin_pct=WORN_SKIN_THRESHOLD)

    assert len(just_below.candidates) == 1, "below the line: treated as a flat-lay"
    assert len(at_threshold.candidates) == 2, "at the line: parsed as worn"


# ---------------------------------------------- measured against real photos


def test_worn_ethnic_wear_is_not_mistaken_for_a_flat_lay() -> None:
    """THE BUG REAL PHOTOGRAPHS FOUND, PINNED SO IT CANNOT COME BACK.

    Until 2026-09-17 every image this pipeline had ever seen was procedurally
    generated. The first eight real photographs broke it immediately:
    `WORN_SKIN_THRESHOLD` was 0.03, and all four worn ethnic-wear photos
    measured BELOW that, so each was treated as a flat-lay and rule 5 dropped
    every mask.

    The cause is structural rather than a tuning accident. A kurta, sherwani or
    fully draped saree covers the wrist, the neck and the ankle; a t-shirt and
    jeans do not. A skin-fraction threshold calibrated on Western clothing
    encodes an assumption about how much of a person their clothes leave
    visible, and that assumption does not survive contact with ethnic wear —
    which is the wardrobe this product exists for.

    The numbers are the ones actually measured by services/stylist_ml.
    """
    from stylist_domain.split import WORN_SKIN_THRESHOLD

    worn_ethnic = {
        "saree (face, shoulders and arms visible)": 0.0213,
        "kurta, full sleeve and high collar": 0.0185,
        "salwar kameez": 0.0123,
        "sherwani": 0.0088,
    }
    for description, skin_pct in worn_ethnic.items():
        assert skin_pct >= WORN_SKIN_THRESHOLD, (
            f"{description} measured {skin_pct} and would be split as a flat-lay; "
            "this is the exact failure that made every worn ethnic photo "
            "collapse to one whole-frame garment"
        )

    # And the other direction still holds: a true flat-lay must NOT be read as
    # worn, or rule 5 stops suppressing the garbage masks it exists for. Every
    # true flat-lay measured exactly 0.0000, including one shot on beige carpet.
    for description, skin_pct in {
        "four garments folded on beige carpet": 0.0,
        "dress shirt, product shot": 0.0,
        "t-shirt, product shot": 0.0,
    }.items():
        assert skin_pct < WORN_SKIN_THRESHOLD, f"{description} would be split"


# ------------------------------------------- multi-garment flat-lays


def _components(*boxes: tuple[int, int, int, int]) -> tuple[Any, ...]:
    from stylist_domain.split import ComponentInfo

    return tuple(ComponentInfo(bbox=b, area_pct=0.1, index=i) for i, b in enumerate(boxes))


def test_a_flat_lay_of_four_garments_becomes_four_garments() -> None:
    """THE GAP REAL PHOTOGRAPHS FOUND.

    Rule 5 refuses to PARSE a flat-lay, because SegFormer is a human-parsing
    model with no human to parse and its class labels are shape guesses. That
    was right. But it also refused to COUNT, so a photo of four folded garments
    catalogued as one garment.

    The labels being wrong does not make the pixels wrong. Measured 2026-09-17
    on a real photo of four folded garments: the union of five mislabelled
    masks has exactly four connected components, positioned correctly. These
    are those boxes.
    """
    from stylist_domain.split import MaskInfo, split_masks

    masks = [
        MaskInfo(atr_label=lbl, slot_hint=None, area_pct=0.2, bbox=(0, 0, 10, 10), index=i)
        for i, lbl in enumerate(["Upper-clothes", "Pants", "Skirt", "Bag", "Hat"])
    ]
    result = split_masks(
        masks,
        skin_pct=0.0,  # a flat-lay
        components=_components(
            (182, 14, 539, 348), (604, 32, 843, 418), (547, 473, 839, 777), (280, 398, 494, 637)
        ),
    )
    assert len(result.candidates) == 4, "four folded garments must not collapse to one"

    # Still no slot hints. A component says WHERE a garment is, never WHAT it
    # is — inventing a slot here would reintroduce the error rule 5 prevents.
    assert all(c.slot_hint is None for c in result.candidates)
    # And each carries its own component so the matte can crop to it.
    assert sorted(c.component_index or 0 for c in result.candidates) == [0, 1, 2, 3]


def test_one_component_still_yields_one_garment() -> None:
    """The common case, and it must not regress: a single garment on a plain
    background is one component and one garment."""
    from stylist_domain.split import split_masks

    result = split_masks([], skin_pct=0.0, components=_components((76, 3, 510, 781)))
    assert len(result.candidates) == 1
    assert result.candidates[0].bbox == (76, 3, 510, 781)


def test_no_components_falls_back_to_the_whole_frame() -> None:
    """A version skew — an older ml image that does not report components — must
    degrade to the previous behaviour rather than produce zero garments."""
    from stylist_domain.split import split_masks

    result = split_masks([], skin_pct=0.0, components=())
    assert len(result.candidates) == 1
    assert result.candidates[0].bbox == (0, 0, 0, 0), "whole-frame sentinel"
    assert result.candidates[0].component_index is None


def test_a_print_inside_a_garment_is_not_a_second_garment() -> None:
    """A contrasting panel — a print, embroidery, zari work — mattes as its own
    blob inside the garment. Emitting it would put a phantom garment in the
    wardrobe whose cutout is somebody's chest print.

    This taxonomy has `embroidered`, `zari_work`, `block_print` and `sequinned`
    in it, so contrasting panels are the norm here, not an edge case.
    """
    from stylist_ml.segmentation import _drop_contained

    garment = {"bbox": [100, 100, 500, 900], "area_pct": 0.30}
    print_panel = {"bbox": [200, 300, 400, 500], "area_pct": 0.04}  # fully inside
    kept = _drop_contained([garment, print_panel])
    assert len(kept) == 1, "the print must not become a garment"
    assert kept[0]["bbox"] == [100, 100, 500, 900]


def test_two_garments_side_by_side_both_survive() -> None:
    """The guard must not merge genuinely separate garments. Overlapping boxes
    with neither contained in the other is two shirts lying next to each
    other, not a print on one of them."""
    from stylist_ml.segmentation import _drop_contained

    left = {"bbox": [0, 0, 500, 400], "area_pct": 0.2}
    right = {"bbox": [450, 0, 900, 400], "area_pct": 0.2}
    assert len(_drop_contained([left, right])) == 2
