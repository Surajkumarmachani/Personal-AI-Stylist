"""Outfit boards: a flat-lay composed from the user's own cutouts (Phase 8).

THE DEFAULT VISUALISATION, AND WHY IT IS NOT A TRY-ON
-----------------------------------------------------
The plan is specific: "always pixel-accurate to the user's actual clothes".
A board shows the garments you own, photographed by you, arranged. It cannot
hallucinate a drape, a fit or a colour, which is exactly what makes it the
DEFAULT while VTON (Phase 10) stays an enhancement that must degrade back to
this. A board is never wrong about what you own; a generated image can be.

PURE: BYTES IN, BYTES OUT
-------------------------
No storage, no database, no network — the caller fetches the cutouts and hands
them over. That keeps `stylist_domain` importable without infrastructure (the
boundary `tests/test_domain_purity.py` enforces) and makes the layout testable
without a bucket.

SLOT-AWARE, NOT A GRID
----------------------
A grid of four squares is not a flat-lay, it is a contact sheet. An outfit
reads as an outfit when the top sits above the bottom and the shoes sit under
both — the spatial arrangement IS the information. Layout is therefore keyed by
slot, and the boxes below are normalised fractions of the canvas so the same
plan renders at any resolution.

DETERMINISTIC, WHICH IS WHAT MAKES CACHING SOUND
------------------------------------------------
The same garment set produces a byte-identical board. Boards are cached in
object storage under `garment_set_hash`, so a layout that depended on dict
order or on wall-clock would serve a cached image that no longer matches what
the layout function would produce — a cache that is silently wrong rather than
merely stale.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image

# Portrait, phone-shaped. Boards are looked at on a phone at 07:00, not on a
# desktop, and a landscape canvas wastes half the screen there.
CANVAS_W = 1000
CANVAS_H = 1250

# Normalised (x, y, w, h) per slot, as fractions of the canvas.
#
# Read it top to bottom and it is a person: head, then the torso layers side by
# side, then legs, then feet, with bag and accessories down the right margin
# where they do not fight the silhouette for attention.
#
# `upper_layer` sits LEFT of `upper_base` rather than on top of it. Overlapping
# them would hide the garment underneath, and a board whose job is "show me
# what I am wearing" must not conceal one of the things being worn.
SLOT_BOXES: dict[str, tuple[float, float, float, float]] = {
    "head": (0.38, 0.01, 0.24, 0.11),
    "upper_layer": (0.02, 0.13, 0.30, 0.30),
    "upper_base": (0.34, 0.13, 0.32, 0.30),
    "drape": (0.68, 0.13, 0.30, 0.34),
    "full_body": (0.28, 0.13, 0.44, 0.52),
    "lower": (0.30, 0.45, 0.36, 0.34),
    "feet": (0.32, 0.81, 0.32, 0.16),
    "bag": (0.02, 0.48, 0.22, 0.20),
    "accessory": (0.78, 0.50, 0.20, 0.14),
}

# Where anything with no box goes. A garment in an unmapped slot is still one
# the user owns, and dropping it silently would make the board disagree with
# the outfit it claims to show.
FALLBACK_BOX = (0.02, 0.70, 0.20, 0.14)

# Slots that can legitimately hold several garments (taxonomy `ranges`).
# Extra items step down the canvas rather than stacking on the same pixels.
MULTI_SLOT_STEP = 0.155


class MissingCutoutError(ValueError):
    """A garment in the outfit has no usable cutout image.

    Raised rather than skipped, DELIBERATELY. A compositor that quietly
    omitted an unrenderable garment would draw a three-garment outfit as two,
    and the result is PLAUSIBLE — it looks like a layout choice, not a failure,
    so nobody investigates. The board's entire claim is that it is accurate to
    what you own; silently showing less of the outfit breaks that claim in the
    one way a viewer cannot detect.

    A garment can reach this state whenever a cutout is missing or corrupt in
    storage: an interrupted matte, a lifecycle rule, a restore that missed the
    bucket. It is not hypothetical, it is just not currently true of any row —
    every `cutout_key` in this database resolves today.
    """


@dataclass(frozen=True)
class Placement:
    """One garment's box, in pixels. The layout plan, before any image work."""

    garment_id: str
    slot: str
    x: int
    y: int
    w: int
    h: int


def plan_layout(
    items: list[tuple[str, str]], *, width: int = CANVAS_W, height: int = CANVAS_H
) -> list[Placement]:
    """Garments -> boxes. Pure, deterministic, no images touched.

    `items` is [(garment_id, slot), ...]. Sorted by slot then id before
    placement so the output cannot depend on the caller's ordering — two
    requests for the same outfit must plan identically or the cached board and
    a freshly rendered one would differ.
    """
    placements: list[Placement] = []
    seen_per_slot: dict[str, int] = {}

    for garment_id, slot in sorted(items, key=lambda t: (t[1], t[0])):
        index = seen_per_slot.get(slot, 0)
        seen_per_slot[slot] = index + 1

        fx, fy, fw, fh = SLOT_BOXES.get(slot, FALLBACK_BOX)
        # Second and subsequent garments in a slot step down rather than
        # overlapping. Clamped so a wardrobe with five accessories does not
        # push the last one off the canvas.
        fy = min(fy + index * MULTI_SLOT_STEP, 1.0 - fh)

        placements.append(
            Placement(
                garment_id=garment_id,
                slot=slot,
                x=int(fx * width),
                y=int(fy * height),
                w=int(fw * width),
                h=int(fh * height),
            )
        )
    return placements


def _fit(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    """Scale to fit the box, PRESERVING ASPECT.

    Stretching a garment to fill its box misrepresents the thing the board
    exists to show accurately — a kurta squashed to a square is not the kurta
    the user owns, and "pixel-accurate to your actual clothes" is the entire
    claim this visualisation makes.
    """
    scale = min(box_w / img.width, box_h / img.height)
    size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
    return img.resize(size, Image.Resampling.LANCZOS)


def compose_board(
    items: list[tuple[str, str, bytes]],
    *,
    width: int = CANVAS_W,
    height: int = CANVAS_H,
    background: tuple[int, int, int, int] = (255, 255, 255, 0),
) -> bytes:
    """Render a flat-lay PNG. `items` is [(garment_id, slot, cutout_png), ...].

    Transparent background by default: the board is composited onto whatever
    the client's theme is, and baking white in makes it a bright rectangle in
    dark mode.
    """
    if not items:
        raise ValueError("cannot compose a board with no garments")

    canvas = Image.new("RGBA", (width, height), background)
    by_id = {gid: data for gid, _slot, data in items}

    for place in plan_layout([(gid, slot) for gid, slot, _ in items], width=width, height=height):
        raw = by_id.get(place.garment_id)
        if not raw:
            raise MissingCutoutError(f"garment {place.garment_id} has no cutout bytes")
        try:
            # `opened` is an ImageFile; `img` below is a plain Image after the
            # convert+resize. Separate names because reusing one makes mypy
            # --strict reject the narrowing, and silencing that with a cast
            # would hide a real type change later.
            opened = Image.open(io.BytesIO(raw))
            opened.load()
        except Exception as exc:
            raise MissingCutoutError(
                f"garment {place.garment_id}: unreadable cutout ({exc})"
            ) from exc

        # RGBA always: a cutout SHOULD carry alpha, but a JPEG re-encode
        # somewhere upstream would silently drop it, and pasting an RGB image
        # with an RGBA mask raises rather than producing a visible defect.
        img = _fit(opened.convert("RGBA"), place.w, place.h)

        # Centred within its box, so garments of different aspect ratios sit on
        # a common vertical axis instead of drifting to the left edge.
        ox = place.x + (place.w - img.width) // 2
        oy = place.y + (place.h - img.height) // 2
        canvas.alpha_composite(img, (max(0, ox), max(0, oy)))

    out = io.BytesIO()
    # `optimize` costs a few ms and saves ~20% on a mostly-transparent canvas,
    # which is what every board is. Boards are served from a CDN and written
    # once, so bytes matter more here than encode time.
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()
