"""Compose several cutouts into one labelled grid image (Step 4.2).

BATCHING IS A COST LEVER, AND A LARGE ONE
-----------------------------------------
A VLM call is priced mostly by image tokens plus a fixed per-request overhead.
Six garments in six calls pays that overhead six times and re-sends the same
instruction text six times. Six garments in ONE grid pays it once. §B2 puts VLM
volume at ~0.02 RPS — never the bottleneck — so batching here buys nothing in
throughput and roughly 6x in cost, which is the entire reason to do it.

The cap is 6 rather than "as many as fit" because each cell gets smaller as the
grid grows, and a garment rendered at 200px is a garment the model cannot
identify. Cost saved by a 12-cell grid is repaid immediately in corrections.

CELLS ARE LABELLED IN THE PIXELS
--------------------------------
The label is drawn onto the image, not just described in the prompt. A model
asked to "describe them in order" has to infer a reading order that is obvious
to us and genuinely ambiguous in a 2x3 layout — and a mis-ordered response
assigns every garment's tags to the wrong garment, with every individual value
looking plausible.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageDraw

# Per §B2's batching note and the plan's Step 4.2.
MAX_CELLS = 6
GRID_COLS = 3
CELL_PX = 420
LABEL_PX = 34
# Mid-grey, not white: cutouts are transparent and many garments are white or
# cream. On white they would vanish; on mid-grey both light and dark garments
# keep their edges.
BACKDROP = (128, 128, 128)


@dataclass(frozen=True, slots=True)
class Grid:
    png: bytes
    cells: list[str]  # e.g. ["A1", "A2", "A3", "B1"]
    cell_to_garment: dict[str, str]


def cell_name(index: int) -> str:
    """A1..A3, B1..B3 — row letter, column number."""
    row = "ABCDEFGH"[index // GRID_COLS]
    return f"{row}{index % GRID_COLS + 1}"


def compose(cutouts: list[tuple[str, bytes]]) -> Grid:
    """(garment_id, cutout_png) -> one labelled grid PNG.

    Each cutout is scaled to FIT its cell with aspect ratio preserved. Stretching
    to fill would distort silhouette, and silhouette is most of what
    distinguishes a kurta from a shirt or palazzo from churidar — the exact
    distinctions this call exists to make.
    """
    if not cutouts:
        raise ValueError("no cutouts to compose")
    if len(cutouts) > MAX_CELLS:
        raise ValueError(f"{len(cutouts)} cutouts exceeds the {MAX_CELLS}-cell cap")

    rows = (len(cutouts) + GRID_COLS - 1) // GRID_COLS
    cols = min(len(cutouts), GRID_COLS)
    canvas = Image.new("RGB", (cols * CELL_PX, rows * (CELL_PX + LABEL_PX)), BACKDROP)
    draw = ImageDraw.Draw(canvas)

    cells: list[str] = []
    mapping: dict[str, str] = {}
    for index, (garment_id, png) in enumerate(cutouts):
        name = cell_name(index)
        cells.append(name)
        mapping[name] = garment_id

        col, row = index % GRID_COLS, index // GRID_COLS
        x0 = col * CELL_PX
        y0 = row * (CELL_PX + LABEL_PX)

        with Image.open(io.BytesIO(png)) as cut:
            rgba = cut.convert("RGBA")
            rgba.thumbnail((CELL_PX - 16, CELL_PX - 16), Image.Resampling.LANCZOS)
            # Composite onto the backdrop so transparency does not become
            # black in the JPEG-ish encoding a provider may apply.
            canvas.paste(
                rgba,
                (
                    x0 + (CELL_PX - rgba.width) // 2,
                    y0 + LABEL_PX + (CELL_PX - rgba.height) // 2,
                ),
                rgba,
            )

        draw.rectangle([x0, y0, x0 + CELL_PX - 1, y0 + LABEL_PX - 1], fill=(32, 32, 32))
        draw.text((x0 + 10, y0 + 8), name, fill=(255, 255, 255))
        draw.rectangle(
            [x0, y0, x0 + CELL_PX - 1, y0 + CELL_PX + LABEL_PX - 1],
            outline=(64, 64, 64),
            width=2,
        )

    buffer = io.BytesIO()
    # PNG, not JPEG: cell labels are small high-contrast text and JPEG ringing
    # around it is exactly the kind of artefact that turns B1 into 81.
    canvas.save(buffer, format="PNG", optimize=True)
    return Grid(png=buffer.getvalue(), cells=cells, cell_to_garment=mapping)
