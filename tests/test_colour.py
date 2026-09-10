"""Colour extraction against the taxonomy palette (Step 3.3).

`primary_colour` is `tier: local, source: lab_kmeans` in taxonomy.yaml with an
eval floor of 0.92 — the second-highest of any field. It is also free: no model
call, no provider, no budget. So it is worth getting right, and it is entirely
testable against known colours.

The palette's nine ethnic-wear values are the reason this uses ΔE2000 rather
than RGB distance. maroon vs red, rust vs orange, cream_ivory vs white are all
close in RGB and obviously different to a person.
"""

from __future__ import annotations

import numpy as np
import pytest

from stylist_domain.colour import (
    SECONDARY_MIN_SHARE,
    delta_e_2000,
    hex_to_lab,
    read_colours,
    rgb_to_lab,
    srgb_to_linear,
)
from stylist_domain.taxonomy import load_taxonomy


@pytest.fixture(scope="module")
def palette() -> dict[str, str | None]:
    return dict(load_taxonomy().colour_hex)


def solid(rgb: tuple[int, int, int], n: int = 4000) -> np.ndarray:
    return np.tile(np.array(rgb, dtype=np.float64), (n, 1))


def mixture(parts: list[tuple[tuple[int, int, int], int]]) -> np.ndarray:
    return np.concatenate([solid(rgb, n) for rgb, n in parts])


# ------------------------------------------------------- colour science


def test_srgb_transfer_function_is_applied() -> None:
    """Treating 8-bit values as linear is the most common colour bug: it
    shifts every midtone and makes dark colours cluster together."""
    assert srgb_to_linear(np.array([0.0])) == pytest.approx(0.0)
    assert srgb_to_linear(np.array([1.0])) == pytest.approx(1.0)
    # Mid-grey is ~0.216 linear, not 0.5. If this returns 0.5, the transfer
    # function is being skipped.
    assert float(srgb_to_linear(np.array([0.5]))[0]) == pytest.approx(0.2140, abs=1e-3)


def test_lab_of_known_colours_matches_reference_values() -> None:
    white = rgb_to_lab(np.array([255, 255, 255]))
    assert float(white[0]) == pytest.approx(100.0, abs=0.1)
    assert abs(float(white[1])) < 0.02 and abs(float(white[2])) < 0.02

    black = rgb_to_lab(np.array([0, 0, 0]))
    assert float(black[0]) == pytest.approx(0.0, abs=0.01)

    # sRGB pure red: L*≈53.24, a*≈80.09, b*≈67.20 (standard reference).
    red = rgb_to_lab(np.array([255, 0, 0]))
    assert float(red[0]) == pytest.approx(53.24, abs=0.1)
    assert float(red[1]) == pytest.approx(80.09, abs=0.1)
    assert float(red[2]) == pytest.approx(67.20, abs=0.1)


def test_delta_e_of_a_colour_with_itself_is_zero() -> None:
    lab = hex_to_lab("#7B1F2B")
    assert delta_e_2000(lab, lab) == pytest.approx(0.0, abs=1e-9)


def test_delta_e_is_symmetric() -> None:
    a, b = hex_to_lab("#C62828"), hex_to_lab("#7B1F2B")
    assert delta_e_2000(a, b) == pytest.approx(delta_e_2000(b, a))


def test_delta_e_separates_the_palettes_close_ethnic_pairs() -> None:
    """The pairs that motivated ΔE2000 over RGB distance.

    Each is close in RGB and clearly distinct to a person. If the metric cannot
    separate them, a maroon silk saree gets catalogued as red and the
    festive-wear filters stop working.
    """
    pairs = [
        ("#C62828", "#7B1F2B", "red vs maroon"),
        ("#E8712F", "#A8471F", "orange vs rust"),
        ("#FAFAFA", "#F3E9D2", "white vs cream_ivory"),
        ("#3B5A80", "#1F3556", "denim_indigo vs blue_navy"),
    ]
    for first, second, label in pairs:
        delta = delta_e_2000(hex_to_lab(first), hex_to_lab(second))
        # ΔE > 5 is "clearly different" to a normal observer.
        assert delta > 5.0, f"{label}: ΔE2000 is only {delta:.1f}"


# ---------------------------------------------------------- extraction


@pytest.mark.parametrize(
    ("hex_value", "expected"),
    [
        ("#111111", "black"),
        ("#FAFAFA", "white"),
        ("#7B1F2B", "maroon"),
        ("#D2286E", "rani_pink"),
        ("#1F7A82", "teal"),
        ("#D4A017", "mustard"),
        ("#1E8C6B", "emerald"),
        ("#C9A227", "gold"),
        ("#3B5A80", "denim_indigo"),
    ],
)
def test_a_solid_palette_colour_reads_back_as_itself(
    palette: dict[str, str | None], hex_value: str, expected: str
) -> None:
    """Round-trip: feed the palette's own anchor, get its id back.

    A failure here means the extraction pipeline (linearise, Lab, k-means,
    nearest anchor) has a bug, since the answer is by construction exact.
    """
    h = hex_value.lstrip("#")
    rgb = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    reading = read_colours(solid(rgb), palette)
    assert reading.primary == expected
    assert reading.primary_delta_e < 1.0
    assert reading.confidence > 0.9


def test_a_two_tone_garment_reports_both_colours(palette) -> None:
    """A maroon saree with a gold border — the taxonomy's own example of why
    secondary_colour exists."""
    pixels = mixture([((123, 31, 43), 7000), ((201, 162, 39), 3000)])
    reading = read_colours(pixels, palette)
    assert reading.primary == "maroon"
    assert reading.secondary == "gold"
    assert reading.secondary_share is not None
    assert reading.secondary_share > SECONDARY_MIN_SHARE


def test_a_minor_accent_is_not_reported_as_secondary(palette) -> None:
    """GOLDEN_SET_SPEC holds human labellers to the same 15% rule, so the model
    and the ground truth answer the same question. A small logo or button
    should not become the garment's second colour."""
    pixels = mixture([((123, 31, 43), 9600), ((250, 250, 250), 400)])
    reading = read_colours(pixels, palette)
    assert reading.primary == "maroon"
    assert reading.secondary is None


def test_highlight_and_shadow_of_one_colour_stay_one_colour(palette) -> None:
    """Real fabric has folds, so k-means finds several clusters for a single
    garment colour. Those must fold onto one palette id rather than becoming
    primary and secondary of the same shade."""
    pixels = mixture(
        [
            ((123, 31, 43), 4000),
            ((150, 45, 58), 3000),  # lit fold
            ((95, 22, 33), 3000),  # shadowed fold
        ]
    )
    reading = read_colours(pixels, palette)
    assert reading.primary == "maroon"
    assert reading.secondary is None, (
        f"folds of one colour split into {reading.primary}/{reading.secondary}"
    )


def test_a_busy_print_reads_as_multicolour(palette) -> None:
    """taxonomy.yaml has `multicolour` as an explicit escape valve for prints
    and bandhani — which is different from "we could not decide"."""
    pixels = mixture(
        [
            ((198, 40, 40), 2000),
            ((31, 122, 130), 2000),
            ((212, 160, 23), 2000),
            ((46, 93, 58), 2000),
            ((250, 250, 250), 2000),
        ]
    )
    reading = read_colours(pixels, palette)
    assert reading.is_multicolour is True
    assert reading.primary == "multicolour"
    assert reading.secondary is None


def test_extraction_is_deterministic(palette) -> None:
    """A garment's colour must not change when a backfill re-runs.

    Non-determinism here looks exactly like data corruption to a user who
    already corrected the field once.
    """
    pixels = mixture([((123, 31, 43), 5000), ((201, 162, 39), 2000)])
    first = read_colours(pixels, palette)
    second = read_colours(pixels, palette)
    assert first == second


def test_a_colour_far_from_every_anchor_lowers_confidence(palette) -> None:
    """Confidence blends dominance with match quality.

    A 100%-dominant colour that is ΔE 30 from everything in the palette is not
    a confident reading — it means the palette is missing a value, and
    `review_below: 0.80` should route it to the correction UI rather than
    asserting a wrong answer.
    """
    neon = read_colours(solid((57, 255, 20)), palette)  # neon green, not in palette
    anchored = read_colours(solid((30, 140, 107)), palette)  # near emerald
    assert neon.primary_delta_e > anchored.primary_delta_e
    assert neon.confidence < anchored.confidence


def test_no_pixels_is_an_error_not_a_guess(palette) -> None:
    """An empty cutout means matting failed. Returning a default colour would
    silently populate the wardrobe with wrong data."""
    with pytest.raises(ValueError, match="no pixels"):
        read_colours(np.empty((0, 3)), palette)


def test_subsampling_does_not_change_the_answer(palette) -> None:
    """Colour proportions are a property of the distribution, so the 20k cap is
    a performance choice that must not alter results."""
    parts = [((123, 31, 43), 70000), ((201, 162, 39), 30000)]
    reading = read_colours(mixture(parts), palette)
    assert reading.primary == "maroon"
    assert reading.secondary == "gold"


def test_every_palette_anchor_is_its_own_nearest_neighbour(palette) -> None:
    """The strongest available check on the palette itself.

    If two anchors are closer to each other than to themselves — i.e. one
    colour's nearest neighbour is a different id — the palette has a genuine
    collision and one of the two values can never be produced. This validates
    Phase 0's 28-colour choice, not just this module.
    """
    anchors = {cid: hex_to_lab(h) for cid, h in palette.items() if h}
    for cid, lab in anchors.items():
        nearest = min(anchors, key=lambda other: delta_e_2000(lab, anchors[other]))
        assert nearest == cid, f"{cid}'s nearest palette anchor is {nearest}"
