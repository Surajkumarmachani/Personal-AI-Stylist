"""Phase 8 — the outfit board compositor.

Two things are being defended here. That the LAYOUT reads as an outfit (top
above bottom, shoes under both) rather than as a contact sheet, and that the
render is DETERMINISTIC — boards are cached in object storage under
`garment_set_hash`, so a layout that varied with dict order would serve a
cached image the layout function would no longer produce. A stale cache is
annoying; a silently wrong one is not detectable from the outside.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from stylist_domain.board import (
    CANVAS_H,
    CANVAS_W,
    SLOT_BOXES,
    MissingCutoutError,
    compose_board,
    plan_layout,
)


def cutout(w: int = 300, h: int = 400, colour: tuple[int, int, int] = (123, 31, 43)) -> bytes:
    """An RGBA PNG with a transparent margin, like a real matted cutout."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    inner = Image.new("RGBA", (int(w * 0.8), int(h * 0.8)), (*colour, 255))
    img.paste(inner, (int(w * 0.1), int(h * 0.1)))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


OUTFIT = [("g-top", "upper_base"), ("g-bottom", "lower"), ("g-shoes", "feet")]


# --------------------------------------------------------------- layout


def test_the_layout_reads_as_an_outfit_not_a_grid() -> None:
    """Top above bottom, shoes under both. The spatial arrangement IS the
    information — a grid of four squares is a contact sheet, not a flat-lay."""
    places = {p.slot: p for p in plan_layout(OUTFIT)}

    assert places["upper_base"].y < places["lower"].y
    assert places["lower"].y < places["feet"].y
    # And they do not overlap vertically, or the board hides what it shows.
    assert places["upper_base"].y + places["upper_base"].h <= places["feet"].y


def test_an_outer_layer_sits_beside_the_base_not_on_top_of_it() -> None:
    """Overlapping them would conceal the garment underneath, and a board whose
    job is "show me what I am wearing" must not hide one of the things worn."""
    places = {p.slot: p for p in plan_layout([("a", "upper_base"), ("b", "upper_layer")])}
    layer, base = places["upper_layer"], places["upper_base"]
    assert layer.x + layer.w <= base.x, "side by side, not stacked"


def test_the_plan_does_not_depend_on_input_order() -> None:
    """Boards are cached under `garment_set_hash`. If the plan varied with the
    caller's ordering, the cached image and a freshly rendered one would differ
    for the same outfit — a cache that is silently wrong, not merely stale."""
    forward = plan_layout(OUTFIT)
    backward = plan_layout(list(reversed(OUTFIT)))
    assert forward == backward


def test_several_garments_in_one_slot_step_down_rather_than_stacking() -> None:
    """The taxonomy allows up to 5 accessories. Placed at the same box they
    would render as one garment and four invisible ones."""
    places = plan_layout([(f"a{i}", "accessory") for i in range(4)])
    ys = [p.y for p in places]
    assert len(set(ys)) == len(ys), "every accessory has its own position"
    assert ys == sorted(ys)


def test_nothing_is_placed_off_canvas() -> None:
    """Five accessories plus two drapes is legal under the taxonomy ranges, and
    the step-down must clamp rather than push the last one out of frame."""
    items = [(f"a{i}", "accessory") for i in range(5)] + [(f"d{i}", "drape") for i in range(2)]
    for p in plan_layout(items):
        assert p.x >= 0 and p.y >= 0
        assert p.x + p.w <= CANVAS_W
        assert p.y + p.h <= CANVAS_H, f"{p.slot} runs off the bottom"


def test_an_unmapped_slot_still_gets_a_box() -> None:
    """A garment in a slot this layout has never heard of is still one the user
    owns. Dropping it would make the board disagree with the outfit it claims
    to show."""
    places = plan_layout([("x", "brand_new_slot")])
    assert len(places) == 1 and places[0].w > 0


def test_every_taxonomy_slot_has_a_box() -> None:
    """Catches a slot added to taxonomy.yaml without a layout: it would fall
    back to one corner and every outfit using it would look broken."""
    from stylist_domain.slots import base_structures, optional_slots, required_slots

    declared = {s for group in base_structures() for s in group}
    declared |= set(required_slots()) | set(optional_slots())
    assert declared <= set(SLOT_BOXES), f"no board box for: {sorted(declared - set(SLOT_BOXES))}"


# -------------------------------------------------------------- render


def test_a_board_renders_to_a_transparent_png_of_the_right_size() -> None:
    """Transparent, not white: the board is composited onto the client's theme
    and a baked-in white background is a bright rectangle in dark mode."""
    data = compose_board([(gid, slot, cutout()) for gid, slot in OUTFIT])
    img = Image.open(io.BytesIO(data))

    assert img.format == "PNG"
    assert img.size == (CANVAS_W, CANVAS_H)
    assert img.mode == "RGBA"
    assert img.getpixel((2, 2))[3] == 0, "the corner is transparent"


def test_the_render_is_byte_identical_for_the_same_outfit() -> None:
    """What makes caching by `garment_set_hash` sound at all."""
    items = [(gid, slot, cutout()) for gid, slot in OUTFIT]
    assert compose_board(items) == compose_board(list(reversed(items)))


def test_aspect_ratio_is_preserved() -> None:
    """Stretching a garment to fill its box misrepresents the thing the board
    exists to show accurately. A wide garment must stay wide."""
    wide = compose_board([("g", "upper_base", cutout(w=600, h=200, colour=(0, 200, 0)))])
    img = Image.open(io.BytesIO(wide)).convert("RGBA")

    xs, ys = [], []
    for y in range(0, img.height, 4):
        for x in range(0, img.width, 4):
            if img.getpixel((x, y))[3] > 0:
                xs.append(x)
                ys.append(y)
    assert xs and ys
    drawn_ratio = (max(xs) - min(xs)) / (max(ys) - min(ys))
    assert drawn_ratio > 2.0, f"a 3:1 garment rendered at {drawn_ratio:.2f}:1"


def test_a_missing_cutout_fails_loudly_instead_of_drawing_a_hole() -> None:
    """Omitting an unrenderable garment produces a PLAUSIBLE wrong answer — a
    three-garment outfit drawn as two looks like a layout choice rather than a
    failure, so nobody investigates. The board's claim is accuracy to what you
    own, and that is the one way to break it invisibly."""
    with pytest.raises(MissingCutoutError, match="g-shoes"):
        compose_board(
            [
                ("g-top", "upper_base", cutout()),
                ("g-bottom", "lower", cutout()),
                ("g-shoes", "feet", b""),
            ]
        )


def test_an_undecodable_cutout_is_the_same_verdict() -> None:
    """A corrupt object in storage is indistinguishable from a missing one as
    far as the board is concerned, and both must be visible rather than drawn
    around."""
    with pytest.raises(MissingCutoutError, match="unreadable"):
        compose_board([("g", "upper_base", b"not a png at all")])


def test_an_rgb_cutout_does_not_crash_the_composite() -> None:
    """Cutouts SHOULD carry alpha, but a JPEG re-encode upstream drops it, and
    pasting RGB with an RGBA mask raises rather than degrading."""
    buf = io.BytesIO()
    Image.new("RGB", (200, 300), (10, 20, 30)).save(buf, format="PNG")
    data = compose_board([("g", "upper_base", buf.getvalue())])
    assert Image.open(io.BytesIO(data)).size == (CANVAS_W, CANVAS_H)


def test_an_empty_outfit_is_an_error_not_a_blank_board() -> None:
    """A blank canvas is a plausible-looking answer to an impossible request,
    and it would be cached as if it were correct."""
    with pytest.raises(ValueError, match="no garments"):
        compose_board([])


def test_a_board_renders_inside_the_latency_budget() -> None:
    """The plan budgets ~80ms for the compositor. Measured here on a 4-garment
    outfit; the exit criterion's "< 200ms p95 from CDN" is a different number
    that includes the network and is not assertable in a unit test."""
    import time

    items = [(gid, slot, cutout()) for gid, slot in OUTFIT] + [
        ("g-bag", "bag", cutout(w=200, h=200))
    ]
    compose_board(items)  # warm the codec paths

    start = time.perf_counter()
    compose_board(items)
    elapsed_ms = (time.perf_counter() - start) * 1000
    # Generous against the 80ms target: this runs on CI hardware alongside
    # other tests, and a tight bound here would fail for load rather than for
    # a regression. It still catches an order-of-magnitude mistake.
    assert elapsed_ms < 400, f"board took {elapsed_ms:.0f}ms"
