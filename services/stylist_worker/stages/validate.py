"""Stage 1: validate.

Everything here fails CLOSED and terminally. A corrupt file, a decode bomb or a
50MB upload will fail identically on every attempt, so retrying is pure waste
and delays telling the user something actionable.

Three checks that are easy to get wrong:

MAGIC BYTES, NOT THE EXTENSION OR THE CONTENT-TYPE
    Both are attacker-controlled. The declared content type was already
    constrained in the presigned policy, but that is a claim by the client, not
    a fact about the bytes. Sniff the actual header.

DECODE BOMBS
    A 40KB PNG can declare 60000x60000 pixels and allocate ~10GB on decode,
    killing the worker (and, on a shared node, its neighbours). Pillow's
    MAX_IMAGE_PIXELS guard exists for exactly this; we check dimensions from
    the header BEFORE decoding pixels.

MINIMUM SIZE
    A 40x40 thumbnail cannot be matted or segmented usefully. Rejecting it here
    with a clear reason beats producing a garbage cutout the user has to
    puzzle over.
"""

from __future__ import annotations

import io
import logging
from typing import Any

from PIL import Image, UnidentifiedImageError

from stylist_worker.state_machine import IngestState, JobContext, Stage, Terminal

logger = logging.getLogger(__name__)

MAX_BYTES = 12 * 1024 * 1024
MIN_SIDE_PX = 200
# ~40 megapixels. Above this we refuse rather than decode: a legitimate phone
# photo is well under, and anything above is either a bomb or a scan that needs
# downsizing before it reaches us.
MAX_PIXELS = 40_000_000

# Magic byte prefixes for the formats the presign policy allows. HEIC/HEIF are
# ISO-BMFF: the brand sits at offset 4 after a 4-byte box length.
MAGIC_PREFIXES: tuple[bytes, ...] = (
    b"\xff\xd8\xff",  # JPEG
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"RIFF",  # WebP (checked further below)
)
HEIF_BRANDS: tuple[bytes, ...] = (
    b"ftypheic",
    b"ftypheix",
    b"ftyphevc",
    b"ftypmif1",
    b"ftypmsf1",
    b"ftypheim",
    b"ftypheis",
)


def sniff_format(data: bytes) -> str | None:
    """Identify the real format from the leading bytes, or None."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if any(brand in data[4:16] for brand in HEIF_BRANDS):
        return "heif"
    return None


async def _run(ctx: JobContext) -> dict[str, Any]:
    store = ctx.scratch.get("store") or _default_store()
    key = ctx.payload["key"]

    head = store.head(key)
    if head is None:
        # The object vanished between ingest and here (or never existed).
        raise Terminal(IngestState.REJECTED, f"no object at {key}")

    size = int(head.get("ContentLength", 0))
    if size > MAX_BYTES:
        raise Terminal(IngestState.REJECTED, f"file is {size} bytes, limit is {MAX_BYTES}")
    if size == 0:
        raise Terminal(IngestState.REJECTED, "file is empty")

    data = store.get_bytes(key)

    fmt = sniff_format(data)
    if fmt is None:
        raise Terminal(
            IngestState.REJECTED,
            "file is not a JPEG, PNG, WebP or HEIF image (checked by magic bytes)",
        )

    # Header-only inspection first: this does not allocate the pixel buffer.
    try:
        with Image.open(io.BytesIO(data)) as probe:
            width, height = probe.size
            probe_format = (probe.format or "").lower()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise Terminal(IngestState.REJECTED, f"image header is unreadable: {exc}") from exc

    if width * height > MAX_PIXELS:
        raise Terminal(
            IngestState.REJECTED,
            f"image declares {width}x{height} = {width * height} pixels, "
            f"limit is {MAX_PIXELS} (decode-bomb guard)",
        )
    if min(width, height) < MIN_SIDE_PX:
        raise Terminal(
            IngestState.REJECTED,
            f"image is {width}x{height}; the shorter side must be at least {MIN_SIDE_PX}px",
        )

    # Only now actually decode, which is where a truncated or malformed file
    # blows up. Terminal, not retryable — the bytes will not improve.
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
    except Exception as exc:
        raise Terminal(IngestState.REJECTED, f"image will not decode: {exc}") from exc

    logger.info(
        "validated job=%s format=%s size=%dx%d bytes=%d",
        ctx.job_id,
        fmt,
        width,
        height,
        size,
    )
    return {
        "source_bytes": data,
        "format": fmt,
        "pil_format": probe_format,
        "width": width,
        "height": height,
        "size_bytes": size,
    }


def _default_store() -> Any:
    from stylist_worker.deps import get_object_store

    return get_object_store()


# RETRYABLE, despite every validation verdict above being deterministic.
#
# The distinction is which failure is being retried. A bad image raises
# Terminal, which the state machine never retries — so a corrupt JPEG still
# fails once and goes straight to REJECTED. But this stage also does two
# network calls (head, get_bytes), and an S3 timeout raising through here with
# retryable=False would terminally REJECT a perfectly good photo because the
# object store hiccuped. That is a data-loss bug wearing a validation error's
# clothes.
validate_stage = Stage(
    name="validate",
    completed_state=IngestState.VALIDATED,
    run=_run,
    retryable=True,
)
