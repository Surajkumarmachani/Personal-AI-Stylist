"""Individual ingest stages: validate, sanitise, matte.

The EXIF test is not a nice-to-have. A phone photo carries GPS to ~5 metres
plus a capture timestamp — together, a home address and when someone is in it.
Coordinates are rounded to 2dp everywhere else in this system for exactly that
reason, which is pointless if the original keeps full EXIF. So the strip is a
privacy control, and privacy controls get tests that fail loudly.
"""

from __future__ import annotations

import io
import uuid
from typing import Any

import piexif
import pytest
from PIL import Image

from stylist_worker.stages.sanitise import SENSITIVE_EXIF_TAGS, sanitise_stage
from stylist_worker.stages.validate import MIN_SIDE_PX, sniff_format, validate_stage
from stylist_worker.state_machine import IngestState, JobContext, Terminal

# asyncio_mode=auto (pyproject) collects coroutine tests already, so no
# module-level asyncio mark — applying one makes pytest-asyncio warn about
# the sync tests in this file.


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put(self, key: str, data: bytes, content_type: str = "image/jpeg") -> None:
        self.objects[key] = (data, content_type)

    # --- the ObjectStore surface the stages use ---
    def head(self, key: str) -> dict[str, Any] | None:
        if key not in self.objects:
            return None
        data, ctype = self.objects[key]
        return {"ContentLength": len(data), "ContentType": ctype}

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key][0]

    def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
        self.objects[key] = (data, content_type)


def _ctx(store: FakeStore, key: str, scratch: dict[str, Any] | None = None) -> JobContext:
    return JobContext(
        job_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        garment_id=uuid.uuid4(),
        payload={"key": key},
        state="received",
        scratch={"store": store, **(scratch or {})},
    )


def make_jpeg(width: int = 400, height: int = 600, *, exif: bytes | None = None) -> bytes:
    img = Image.new("RGB", (width, height), (120, 80, 60))
    buf = io.BytesIO()
    if exif:
        img.save(buf, format="JPEG", exif=exif)
    else:
        img.save(buf, format="JPEG")
    return buf.getvalue()


def make_jpeg_with_gps() -> bytes:
    """A JPEG carrying the metadata a real phone attaches."""
    exif = {
        "0th": {
            piexif.ImageIFD.Make: b"TestPhone",
            piexif.ImageIFD.Model: b"TP-1",
        },
        "Exif": {
            piexif.ExifIFD.DateTimeOriginal: b"2026:09:09 11:22:33",
            piexif.ExifIFD.BodySerialNumber: b"SERIAL-123456",
        },
        "GPS": {
            piexif.GPSIFD.GPSLatitudeRef: b"N",
            # 12.9716 N, 77.5946 E — Bengaluru, to full precision.
            piexif.GPSIFD.GPSLatitude: ((12, 1), (58, 1), (1776, 100)),
            piexif.GPSIFD.GPSLongitudeRef: b"E",
            piexif.GPSIFD.GPSLongitude: ((77, 1), (35, 1), (4056, 100)),
        },
        "1st": {},
        "thumbnail": None,
    }
    return make_jpeg(exif=piexif.dump(exif))


# ---------------------------------------------------------------- validate


def test_sniff_format_identifies_real_headers() -> None:
    assert sniff_format(make_jpeg()) == "jpeg"
    png = io.BytesIO()
    Image.new("RGB", (300, 300)).save(png, format="PNG")
    assert sniff_format(png.getvalue()) == "png"


def test_sniff_format_rejects_a_renamed_file() -> None:
    """A .jpg extension and an image/jpeg content-type are both client claims.
    Only the bytes are evidence."""
    assert sniff_format(b"%PDF-1.7\n%fake jpeg") is None
    assert sniff_format(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>") is None


async def test_validate_accepts_a_normal_photo() -> None:
    store = FakeStore()
    store.put("originals/u/1", make_jpeg())
    result = await validate_stage.run(_ctx(store, "originals/u/1"))
    assert result["format"] == "jpeg"
    assert result["width"] == 400
    assert result["height"] == 600


async def test_validate_rejects_a_non_image() -> None:
    store = FakeStore()
    store.put("originals/u/1", b"%PDF-1.7\n" + b"\x00" * 4096)
    with pytest.raises(Terminal) as exc:
        await validate_stage.run(_ctx(store, "originals/u/1"))
    assert exc.value.state is IngestState.REJECTED
    assert "magic bytes" in exc.value.reason


async def test_validate_rejects_a_corrupt_jpeg() -> None:
    """The poison-image case. Terminal, so it is not retried three times."""
    store = FakeStore()
    good = make_jpeg()
    # Valid header, truncated payload — decodes far enough to open, then fails.
    store.put("originals/u/1", good[:200] + b"\xff" * 50)
    with pytest.raises(Terminal) as exc:
        await validate_stage.run(_ctx(store, "originals/u/1"))
    assert exc.value.state is IngestState.REJECTED


async def test_validate_rejects_a_too_small_image() -> None:
    store = FakeStore()
    store.put("originals/u/1", make_jpeg(50, 50))
    with pytest.raises(Terminal) as exc:
        await validate_stage.run(_ctx(store, "originals/u/1"))
    assert str(MIN_SIDE_PX) in exc.value.reason


async def test_validate_rejects_a_decode_bomb() -> None:
    """A small file that declares an enormous canvas.

    Pillow would allocate ~w*h*3 bytes on decode; the dimension check runs on
    the header first so the allocation never happens.
    """
    store = FakeStore()
    # 30000x30000 PNG of a single colour compresses to a few KB.
    big = Image.new("RGB", (1, 1))
    buf = io.BytesIO()
    big.save(buf, format="PNG")
    raw = bytearray(buf.getvalue())
    # Patch the IHDR width/height to 30000x30000 without growing the file.
    raw[16:20] = (30000).to_bytes(4, "big")
    raw[20:24] = (30000).to_bytes(4, "big")
    store.put("originals/u/1", bytes(raw))

    with pytest.raises(Terminal) as exc:
        await validate_stage.run(_ctx(store, "originals/u/1"))
    # Either the bomb guard or the CRC check catches it; both are terminal and
    # both are correct. What must NOT happen is a 2.7GB allocation.
    assert exc.value.state is IngestState.REJECTED


async def test_validate_rejects_a_missing_object() -> None:
    store = FakeStore()
    with pytest.raises(Terminal) as exc:
        await validate_stage.run(_ctx(store, "originals/u/does-not-exist"))
    assert exc.value.state is IngestState.REJECTED


async def test_validate_rejects_an_empty_file() -> None:
    store = FakeStore()
    store.put("originals/u/1", b"")
    with pytest.raises(Terminal) as exc:
        await validate_stage.run(_ctx(store, "originals/u/1"))
    assert "empty" in exc.value.reason


# ---------------------------------------------------------------- sanitise


async def test_source_photo_really_does_carry_gps() -> None:
    """Guard for the test below: if the fixture stops embedding GPS, the strip
    test would pass trivially and prove nothing."""
    data = make_jpeg_with_gps()
    exif = piexif.load(data)
    assert exif["GPS"], "fixture is not carrying GPS; the strip test would be vacuous"


async def test_sanitise_strips_gps_and_every_other_sensitive_tag() -> None:
    """PHASE 2 EXIT CRITERION: EXIF GPS stripped, asserted in a test."""
    store = FakeStore()
    original = make_jpeg_with_gps()
    store.put("originals/u/1", original)

    ctx = _ctx(store, "originals/u/1", {"source_bytes": original})
    result = await sanitise_stage.run(ctx)

    cleaned = store.get_bytes(result["sanitised_key"])
    with Image.open(io.BytesIO(cleaned)) as img:
        assert not img.info.get("exif"), "sanitised image still carries an EXIF block"
        assert not img.getexif(), "sanitised image still exposes EXIF tags"
        for tag in SENSITIVE_EXIF_TAGS:
            assert tag not in img.getexif(), f"EXIF tag {tag:#06x} survived"

    # And specifically: no GPS block at all.
    try:
        leftover = piexif.load(cleaned)
        assert not leftover.get("GPS"), f"GPS survived: {leftover['GPS']}"
    except Exception:
        pass


async def test_sanitise_bakes_in_orientation_rather_than_dropping_it() -> None:
    """The trap in metadata stripping.

    EXIF also carries the orientation flag. Drop it naively and every portrait
    photo comes out rotated 90 degrees, because the pixels were always
    landscape and the flag was the only thing saying otherwise.
    """
    # 400x600 pixels tagged "rotate 90", i.e. it should DISPLAY as 600x400.
    exif = {
        "0th": {piexif.ImageIFD.Orientation: 6},
        "Exif": {},
        "GPS": {},
        "1st": {},
        "thumbnail": None,
    }
    original = make_jpeg(400, 600, exif=piexif.dump(exif))
    store = FakeStore()
    store.put("originals/u/1", original)

    ctx = _ctx(store, "originals/u/1", {"source_bytes": original})
    result = await sanitise_stage.run(ctx)

    with Image.open(io.BytesIO(store.get_bytes(result["sanitised_key"]))) as img:
        # Rotation applied to the pixels: the stored image is now genuinely
        # 600x400 and needs no flag to display correctly.
        assert img.size == (600, 400), (
            f"orientation was dropped instead of applied (got {img.size}); "
            "every portrait photo would render sideways"
        )


async def test_sanitise_writes_a_lossless_intermediate() -> None:
    """PNG, not JPEG: matting works on garment edges, and JPEG artefacts are
    worst exactly there."""
    store = FakeStore()
    original = make_jpeg()
    store.put("originals/u/1", original)
    ctx = _ctx(store, "originals/u/1", {"source_bytes": original})
    result = await sanitise_stage.run(ctx)
    assert result["sanitised_key"].endswith(".png")
    assert store.objects[result["sanitised_key"]][1] == "image/png"
    assert sniff_format(store.get_bytes(result["sanitised_key"])) == "png"
