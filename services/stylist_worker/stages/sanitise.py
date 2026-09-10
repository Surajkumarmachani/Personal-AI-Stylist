"""Stage 2: sanitise — strip EXIF, keep orientation.

THIS IS A PRIVACY CONTROL, WHICH IS WHY IT HAS ITS OWN TEST
-----------------------------------------------------------
A phone photo carries GPS coordinates to ~5 metres, plus a capture timestamp,
device serial and often the owner's name. Every one of those follows the image
into the object store, the CDN, and — for the two stages that export pixels —
a third party's infrastructure. Coordinates are rounded to 2dp everywhere else
in this system specifically to avoid holding a home address; keeping full EXIF
on the original would make that rounding pointless.

Stripping metadata by re-encoding has one trap: EXIF also carries the
ORIENTATION flag. Drop it naively and every photo taken in portrait comes out
rotated 90 degrees, because the pixels were always landscape and the flag was
the only thing saying otherwise. So orientation is APPLIED to the pixels first
(baking the rotation in), then all metadata is discarded.
"""

from __future__ import annotations

import io
import logging
from typing import Any

from PIL import Image, ImageOps

from stylist_worker.state_machine import IngestState, JobContext, Stage

logger = logging.getLogger(__name__)

# Tags whose absence we assert in tests. Not exhaustive — the strip is
# whitelist-based (we copy pixels, not metadata), so anything not listed is
# also gone. These are the ones that matter most if the strip ever regresses.
SENSITIVE_EXIF_TAGS: tuple[int, ...] = (
    0x8825,  # GPSInfo
    0x9003,  # DateTimeOriginal
    0xA430,  # CameraOwnerName
    0xA431,  # BodySerialNumber
    0x010F,  # Make
    0x0110,  # Model
)


async def _run(ctx: JobContext) -> dict[str, Any]:
    store = ctx.scratch.get("store") or _default_store()
    data: bytes = ctx.scratch["source_bytes"]

    with Image.open(io.BytesIO(data)) as opened:
        # Bake the EXIF orientation into the pixels BEFORE discarding metadata.
        # exif_transpose returns a new Image (not an ImageFile), hence the
        # separate name rather than rebinding `opened`.
        img: Image.Image = ImageOps.exif_transpose(opened) or opened
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")

        # A fresh image built from RAW PIXEL BYTES only. Nothing crosses over
        # except the pixels, so there is no metadata to miss and no allowlist
        # to keep up to date.
        #
        # frombytes(tobytes()) rather than putdata(getdata()): getdata()
        # materialises a Python list of one tuple per pixel — ~12 million
        # tuples for a phone photo, which is both slow and pointless when the
        # buffer can be handed over directly. (It is also deprecated in
        # Pillow 12 and removed in 14.)
        clean = Image.frombytes(img.mode, img.size, img.tobytes())

        buffer = io.BytesIO()
        # PNG for the intermediate: lossless, so matting is not fighting JPEG
        # artefacts at garment edges, which is exactly where the alpha matters.
        clean.save(buffer, format="PNG", optimize=False)
        sanitised = buffer.getvalue()

    original_key: str = ctx.payload["key"]
    sanitised_key = f"sanitised/{ctx.user_id}/{ctx.job_id}.png"
    store.put_bytes(sanitised_key, sanitised, content_type="image/png")

    logger.info(
        "sanitised job=%s exif_stripped bytes=%d -> %d",
        ctx.job_id,
        len(data),
        len(sanitised),
    )
    return {
        "sanitised_key": sanitised_key,
        "sanitised_bytes": sanitised,
        "original_key": original_key,
    }


def _default_store() -> Any:
    from stylist_worker.deps import get_object_store

    return get_object_store()


sanitise_stage = Stage(
    name="sanitise",
    completed_state=IngestState.SANITISED,
    run=_run,
    retryable=True,
)
