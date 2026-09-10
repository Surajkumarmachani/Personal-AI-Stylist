"""Regressions for four bugs that shipped past the earlier test suites.

Each of these passed every test that existed at the time. They are grouped here
because they share a cause: the tests exercised the CONTROL FLOW around the
code rather than the code's actual data contracts.

  1. Resume was broken. `test_resume_skips_completed_stages` used fake stages,
     so it proved the state machine skips completed work — and never that a
     real stage could run without the previous stage's in-memory output.
     Resuming a job at MODERATED raised KeyError('sanitised_key').
  2. ORM enums had no values, so SQLAlchemy could write `slot='lower'` and then
     fail to READ it back. Hidden through two phases because every enum column
     was NULL until segmentation started setting one.
  3. numpy scalars leaked into SQL parameters. `np.float64 < float` is
     `np.bool_`, which asyncpg rejects — surfacing as a DataError on an UPDATE,
     three layers from the cause.
  4. Masked matting took its RGB from rembg's output, which zeroes colour
     where its own alpha is zero. Widening the alpha with a mask then revealed
     BLACK, so a maroon top was catalogued as `black`.
"""

from __future__ import annotations

import io
import uuid
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select, text

from stylist_db.models import Garment
from stylist_domain.colour import read_colours
from stylist_domain.taxonomy import load_taxonomy


class FakeStore:
    """Only what the stages actually call."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put(self, key: str, data: bytes, ctype: str = "image/png") -> None:
        self.objects[key] = (data, ctype)

    def head(self, key: str) -> dict[str, Any] | None:
        if key not in self.objects:
            return None
        return {"ContentLength": len(self.objects[key][0])}

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key][0]

    def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
        self.objects[key] = (data, content_type)


def jpeg(colour: tuple[int, int, int] = (123, 31, 43)) -> bytes:
    img = Image.new("RGB", (500, 700), (238, 235, 230))
    ImageDraw.Draw(img).rounded_rectangle([120, 150, 380, 560], radius=30, fill=colour)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


# ---------------------------------------------------------- 1. resume


async def test_sanitise_runs_without_the_previous_stages_scratch() -> None:
    """THE RESUME BUG.

    On a resumed job, validate is skipped as already-done, so
    `scratch['source_bytes']` does not exist. Every stage must be able to fetch
    its input from durable storage instead — a stage that requires scratch
    works only on a first run, which is precisely the case a crash excludes.
    """
    from stylist_worker.stages.sanitise import sanitise_stage
    from stylist_worker.state_machine import JobContext

    store = FakeStore()
    user_id, job_id = uuid.uuid4(), uuid.uuid4()
    key = f"originals/{user_id}/upload"
    store.put(key, jpeg(), "image/jpeg")

    ctx = JobContext(
        job_id=job_id,
        user_id=user_id,
        garment_id=uuid.uuid4(),
        payload={"key": key},
        state="validated",
        scratch={"store": store},  # empty of stage output, as after a restart
    )
    result = await sanitise_stage.run(ctx)
    assert result["sanitised_key"] in store.objects


async def test_load_sanitised_falls_back_to_the_object_store() -> None:
    from stylist_worker.io_helpers import load_sanitised
    from stylist_worker.keys import sanitised_key
    from stylist_worker.state_machine import JobContext

    store = FakeStore()
    user_id, job_id = uuid.uuid4(), uuid.uuid4()
    store.put(sanitised_key(user_id, job_id), jpeg())

    ctx = JobContext(
        job_id=job_id,
        user_id=user_id,
        garment_id=uuid.uuid4(),
        payload={"key": "originals/x/y"},
        state="moderated",
        scratch={},
    )
    assert len(load_sanitised(ctx, store)) > 0


async def test_a_missing_intermediate_is_terminal_not_a_crash() -> None:
    """If the intermediate is genuinely gone, say so with a user-visible
    reason. The state machine will not re-run a completed stage, so silently
    continuing would produce a half-result."""
    from stylist_worker.io_helpers import load_sanitised
    from stylist_worker.state_machine import IngestState, JobContext, Terminal

    ctx = JobContext(
        job_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        garment_id=uuid.uuid4(),
        payload={"key": "originals/x/y"},
        state="moderated",
        scratch={},
    )
    with pytest.raises(Terminal) as exc:
        load_sanitised(ctx, FakeStore())
    assert exc.value.state is IngestState.REJECTED


def test_object_keys_are_derivable_from_ids_alone() -> None:
    """The property that makes resume possible: any worker can compute where
    the bytes are without a handover from the stage that wrote them."""
    from stylist_worker.keys import cutout_key, mask_key, sanitised_key

    user_id, job_id, garment_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert sanitised_key(user_id, job_id) == sanitised_key(user_id, job_id)
    # Cutouts are per-GARMENT: one photo can yield several, and a job-keyed
    # path would have them overwrite each other.
    assert cutout_key(user_id, garment_id) != cutout_key(user_id, uuid.uuid4())
    for key in (
        sanitised_key(user_id, job_id),
        cutout_key(user_id, garment_id),
        mask_key(user_id, garment_id),
    ):
        assert str(user_id) in key, "keys must be tenant-prefixed for prefix deletion (§C5)"


# ------------------------------------------------------- 2. ORM enums


async def test_orm_can_read_back_every_enum_value_it_can_write(
    app_sessionmaker, two_tenants
) -> None:
    """THE ENUM BUG.

    `ENUM(name="slot", create_type=False)` with no values lets SQLAlchemy WRITE
    a value it does not know and then throw on READ:

        LookupError: 'lower' is not among the defined enum values.
        Enum name: slot. Possible values: None

    It stayed hidden through Phases 1-2 because every enum column was NULL, so
    the first symptom was the wardrobe endpoint 500ing on a garment that had
    saved perfectly well.
    """
    tenant_a, _ = two_tenants
    taxonomy = load_taxonomy()

    async with app_sessionmaker() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(tenant_a)}
        )
        # One row per slot, so every enum label is exercised on the way back.
        for slot in taxonomy.slots:
            await session.execute(
                text(
                    "INSERT INTO garments (id, user_id, original_key, slot, state) "
                    "VALUES (:id, :uid, :key, CAST(:slot AS slot), 'segmented')"
                ),
                {
                    "id": uuid.uuid4(),
                    "uid": tenant_a,
                    "key": f"originals/{tenant_a}/{slot}",
                    "slot": slot,
                },
            )

        # The read that used to raise.
        rows = await session.execute(select(Garment).where(Garment.slot.is_not(None)))
        slots = {g.slot for g in rows.scalars()}
        assert slots == set(taxonomy.slots), f"could not read back: {set(taxonomy.slots) - slots}"


def test_orm_enum_values_come_from_the_taxonomy() -> None:
    """Declared from the same loader that generates the Postgres types, so the
    ORM and the database cannot drift apart."""
    from stylist_db.models import ColourEnum, SlotEnum

    taxonomy = load_taxonomy()
    assert set(SlotEnum.enums) == set(taxonomy.slots)
    assert set(ColourEnum.enums) == set(taxonomy.colours)


# ------------------------------------------------ 3. numpy leakage


def test_colour_reading_holds_python_primitives_not_numpy_scalars() -> None:
    """THE NUMPY BUG.

    Shares are `np.int64 / np.int64` -> np.float64, and `np.float64 < float`
    is np.bool_. asyncpg rejects that with

        invalid input for query argument $6: np.False_ (a boolean is required)

    which surfaces as a DataError on an UPDATE, far from the cause. Coercing at
    the dataclass boundary keeps numpy inside the module that uses it.
    """
    palette = dict(load_taxonomy().colour_hex)
    pixels = np.tile(np.array([123, 31, 43], dtype=np.float64), (3000, 1))
    reading = read_colours(pixels, palette)

    for name, value in (
        ("primary_share", reading.primary_share),
        ("primary_delta_e", reading.primary_delta_e),
        ("confidence", reading.confidence),
    ):
        assert type(value) is float, f"{name} is {type(value).__name__}, not float"
    assert type(reading.is_multicolour) is bool

    # The comparison that actually broke: it must yield a real bool.
    assert type(reading.confidence < 0.8) is bool


# ------------------------------------------- 4. masked matte colour


def test_masked_matte_keeps_the_garments_real_colour() -> None:
    """THE BLACK-CUTOUT BUG.

    rembg zeroes RGB wherever its own alpha is 0. Taking RGB from its output
    and then widening the alpha with a segmentation mask reveals those blanked
    pixels, so colour extraction reads `black`. Observed end-to-end: a maroon
    top and denim trousers were both catalogued as `black`.

    Alpha and colour must come from different sources — the alpha from whatever
    we decided, the colour always from the untouched original.
    """
    from stylist_ml import matting

    if not matting.model_available():
        pytest.skip("u2net weights absent")

    maroon = (123, 31, 43)
    image = jpeg(maroon)

    # A mask covering the garment rectangle only.
    mask = Image.new("L", (500, 700), 0)
    ImageDraw.Draw(mask).rounded_rectangle([120, 150, 380, 560], radius=30, fill=255)
    buf = io.BytesIO()
    mask.convert("1").save(buf, format="PNG")

    result = matting.matte(image, mask_png=buf.getvalue())
    with Image.open(io.BytesIO(result.cutout_png)) as cut:
        rgba = np.asarray(cut.convert("RGBA"))

    opaque = rgba[..., 3] >= 128
    assert opaque.sum() > 0, "the mask fallback did not preserve any garment pixels"

    mean = rgba[opaque][:, :3].mean(axis=0)
    # Maroon, not black. Generous tolerance — JPEG and edge feathering shift it
    # — but black (near 0) fails clearly.
    assert mean[0] > 60, f"cutout is too dark to be maroon: mean RGB {mean.round(0)}"
    assert mean[0] > mean[2], f"red should dominate blue for maroon: {mean.round(0)}"


def test_masked_matte_falls_back_when_u2net_finds_nothing() -> None:
    """u2net is unreliable on a tight crop with little context; measured once at
    an alpha sum of 17 against the mask's 48,793 over the same region.

    Intersecting with that loses a garment segmentation had located correctly,
    so the mask is used alone when u2net contradicts it. This is matting's
    fallback (§C6): every stage has one.
    """
    from stylist_ml import matting

    if not matting.model_available():
        pytest.skip("u2net weights absent")

    # A tiny flat patch — the case u2net handles worst.
    img = Image.new("RGB", (300, 300), (240, 238, 234))
    ImageDraw.Draw(img).rectangle([100, 100, 200, 200], fill=(31, 122, 130))
    src = io.BytesIO()
    img.save(src, format="PNG")

    mask = Image.new("L", (300, 300), 0)
    ImageDraw.Draw(mask).rectangle([100, 100, 200, 200], fill=255)
    mbuf = io.BytesIO()
    mask.convert("1").save(mbuf, format="PNG")

    result = matting.matte(src.getvalue(), mask_png=mbuf.getvalue())
    # Whatever u2net decided, the garment survives.
    assert result.alpha_coverage > 0.10, (
        f"garment lost: coverage {result.alpha_coverage:.2%} — the mask fallback did not engage"
    )
