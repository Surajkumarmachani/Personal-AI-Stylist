"""Real ONNX inference: segmentation, embedding, masked matting.

Skipped when weights are absent (`python scripts/download_models.py` fetches
~650MB), so a contributor without them still gets a green suite. CI fetches
them and caches them, which is the environment that has to catch a regression.

Fixtures are synthetic figures rather than photographs. That is a deliberate
limit on what these tests claim: they assert the PLUMBING — correct tensor
shapes, upsampling order, mask geometry, normalised vectors — not accuracy.
Accuracy is what the golden set measures in Step 3.5, and no amount of
synthetic imagery substitutes for 500 labelled photos.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image, ImageDraw

from stylist_ml import registry
from stylist_ml.runtime import ModelUnavailable, load

pytestmark = pytest.mark.skipif(
    not (registry.SEGFORMER.available() and registry.FASHION_SIGLIP.available()),
    reason="model weights absent; run scripts/download_models.py",
)


def _load_and_release(spec):
    """Yield a loaded model, then drop the ONNX session deterministically.

    Left to interpreter shutdown, onnxruntime's native session destructors race
    its thread-pool teardown and the process aborts with

        libc++abi: terminating due to uncaught exception of type
        std::__1::system_error: recursive_mutex lock failed

    AFTER every test has passed. pytest reports "162 passed" and exits 134 — a
    red build on a green run. Three sessions now exist (u2net, SegFormer,
    FashionSigLIP), so each one has to be released by whoever owns it.
    """
    import gc

    try:
        model = load(spec)
    except ModelUnavailable as exc:  # pragma: no cover - guarded by skipif
        pytest.skip(str(exc))
    yield model
    del model.session
    del model
    gc.collect()


@pytest.fixture(scope="module", autouse=True)
def _release_matting_session():
    """Release the u2net session too — the one nobody owned.

    _load_and_release covers the sessions the fixtures build, but matting's
    session is held by an lru_cache on `matting._session`, so it outlives every
    fixture and its native destructor runs during interpreter shutdown. That is
    the same race described above, and it is why this file could abort with

        recursive_mutex lock failed: Invalid argument

    AFTER reporting "10 passed" — intermittently, since it depends on whether
    the destructor happens to lose the race with thread-pool teardown, which
    made it show up only on a loaded machine. Clearing the cache drops the last
    reference while the interpreter is still fully alive.
    """
    import gc

    yield
    from stylist_ml import matting

    matting._session.cache_clear()
    gc.collect()


@pytest.fixture(scope="module")
def seg_model():
    yield from _load_and_release(registry.SEGFORMER)


@pytest.fixture(scope="module")
def embed_model():
    yield from _load_and_release(registry.FASHION_SIGLIP)


def worn_figure(width: int = 600, height: int = 900) -> bytes:
    """A crude person wearing a top, trousers and shoes.

    Enough structure for several ATR classes to fire, which is what the split
    logic in Step 3.2 will consume.
    """
    img = Image.new("RGB", (width, height), (240, 238, 234))
    d = ImageDraw.Draw(img)
    d.ellipse([260, 60, 340, 150], fill=(226, 190, 160))
    d.rounded_rectangle([215, 155, 385, 470], radius=25, fill=(123, 31, 43))
    d.rectangle([240, 470, 295, 760], fill=(59, 90, 128))
    d.rectangle([305, 470, 360, 760], fill=(59, 90, 128))
    d.rounded_rectangle([232, 760, 300, 800], radius=8, fill=(30, 30, 30))
    d.rounded_rectangle([300, 760, 368, 800], radius=8, fill=(30, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def flat_lay(colour: tuple[int, int, int] = (123, 31, 43)) -> bytes:
    img = Image.new("RGB", (700, 900), (238, 235, 230))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([190, 240, 510, 700], radius=40, fill=colour)
    d.polygon([(190, 260), (110, 380), (175, 430), (215, 320)], fill=colour)
    d.polygon([(510, 260), (590, 380), (525, 430), (485, 320)], fill=colour)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


# ------------------------------------------------------------- segmentation


def test_segment_finds_the_garments_in_a_worn_figure(seg_model) -> None:
    from stylist_ml.segmentation import segment

    result = segment(seg_model, worn_figure())
    labels = {m.atr_label for m in result.masks}
    assert "Upper-clothes" in labels
    assert "Pants" in labels


def test_masks_are_reported_at_the_original_resolution(seg_model) -> None:
    """Not at the model's 512x512 input, and not at its 128x128 logit grid.

    A mask the caller has to rescale is a mask the caller can rescale WRONG —
    and resizing a label map interpolates between class ids, which produces
    pixels belonging to a garment class nobody detected.
    """
    from stylist_ml.segmentation import segment

    result = segment(seg_model, worn_figure(width=613, height=877))
    assert (result.width, result.height) == (613, 877)
    for mask in result.masks:
        with Image.open(io.BytesIO(mask.mask_png)) as img:
            assert img.size == (613, 877), f"{mask.atr_label} mask is {img.size}"


def test_mask_bboxes_are_inside_the_frame_and_match_the_mask(seg_model) -> None:
    from stylist_ml.segmentation import segment

    result = segment(seg_model, worn_figure())
    for mask in result.masks:
        x1, y1, x2, y2 = mask.bbox
        assert 0 <= x1 < x2 <= result.width
        assert 0 <= y1 < y2 <= result.height
        with Image.open(io.BytesIO(mask.mask_png)) as img:
            assert np.asarray(img.convert("L"), dtype=bool)[y1:y2, x1:x2].any()


def test_areas_are_fractions_that_cannot_exceed_the_frame(seg_model) -> None:
    """Classes are mutually exclusive per pixel, so garment areas plus
    non-garment coverage cannot exceed 1. Exceeding it would mean the label map
    is being double-counted."""
    from stylist_ml.segmentation import segment

    result = segment(seg_model, worn_figure())
    total = sum(m.area_pct for m in result.masks) + sum(result.non_garment_coverage.values())
    assert all(0.0 < m.area_pct <= 1.0 for m in result.masks)
    assert total <= 1.0001, f"coverage sums to {total}"


def test_background_is_never_returned_as_a_garment(seg_model) -> None:
    from stylist_ml.segmentation import segment

    result = segment(seg_model, flat_lay())
    assert "Background" not in {m.atr_label for m in result.masks}
    assert "Background" in result.non_garment_coverage


def test_left_and_right_shoes_come_back_separately(seg_model) -> None:
    """Merging them into one garment is Step 3.2's dedupe stage, not this
    service's job — and doing it here would throw away the geometry the merge
    needs."""
    from stylist_ml.segmentation import segment

    result = segment(seg_model, worn_figure())
    shoes = [m for m in result.masks if m.atr_label in ("Left-shoe", "Right-shoe")]
    if shoes:  # depends on the model actually detecting them in a synthetic figure
        assert len({m.atr_label for m in shoes}) == len(shoes), "shoes were merged"


def test_segment_tolerates_an_rgba_cutout(seg_model) -> None:
    """The pipeline may segment an already-matted image; a 4-channel array
    would be a shape error against a 3-channel model."""
    from stylist_ml.segmentation import segment

    img = Image.new("RGBA", (400, 500), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([100, 120, 300, 380], radius=20, fill=(31, 122, 130, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    result = segment(seg_model, buf.getvalue())
    assert (result.width, result.height) == (400, 500)


# ---------------------------------------------------------------- embedding


def test_embed_returns_a_768d_unit_vector(embed_model) -> None:
    from stylist_ml.embedding import embed

    result = embed(embed_model, flat_lay())
    assert result.dim == registry.EMBEDDING_DIM
    assert len(result.vector) == 768
    assert abs(np.linalg.norm(result.vector) - 1.0) < 1e-5


def test_embedding_is_deterministic(embed_model) -> None:
    """Two calls on identical bytes must give identical vectors.

    Non-determinism here would mean a garment's stored vector disagrees with
    what a re-embed produces, so dedupe (cosine > 0.95) would stop recognising
    a photo as a duplicate of itself.
    """
    from stylist_ml.embedding import embed

    a = embed(embed_model, flat_lay())
    b = embed(embed_model, flat_lay())
    assert np.allclose(a.vector, b.vector, atol=1e-6)


def test_different_garments_are_further_apart_than_recolours(embed_model) -> None:
    """A weak but real sanity check on the embedding space.

    Not an accuracy claim — it only asserts the vectors respond to image
    content at all. An embedding that returned near-identical vectors for
    everything would pass every other test in this file while making vector
    search useless.
    """
    from stylist_ml.embedding import embed

    base = np.array(embed(embed_model, flat_lay((123, 31, 43))).vector)
    recolour = np.array(embed(embed_model, flat_lay((31, 122, 130))).vector)

    trousers = Image.new("RGB", (700, 900), (238, 235, 230))
    d = ImageDraw.Draw(trousers)
    d.rectangle([250, 200, 330, 780], fill=(59, 90, 128))
    d.rectangle([370, 200, 450, 780], fill=(59, 90, 128))
    buf = io.BytesIO()
    trousers.save(buf, format="JPEG", quality=92)
    other = np.array(embed(embed_model, buf.getvalue()).vector)

    assert float(base @ recolour) > float(base @ other), (
        "the same garment in another colour should sit closer than a different "
        "garment type; the embedding is not responding to shape"
    )


# ------------------------------------------------------- masked matting


def test_masked_matte_keeps_only_the_requested_garment(seg_model) -> None:
    """The multi-garment case that makes /matte's mask parameter necessary.

    Without a mask, u2net decides what the subject is from the whole frame — on
    a figure wearing a top and trousers it keeps both, so asking for "the top"
    would return the whole outfit.
    """
    from stylist_ml import matting
    from stylist_ml.segmentation import segment

    if not matting.model_available():
        pytest.skip("u2net weights absent")

    image = worn_figure()
    result = segment(seg_model, image)
    top = next((m for m in result.masks if m.atr_label == "Upper-clothes"), None)
    if top is None:
        pytest.skip("segmentation did not find an upper garment in the fixture")

    masked = matting.matte(image, mask_png=top.mask_png)
    unmasked = matting.matte(image)

    # The mask crops to the garment's bbox, so the masked cutout must be no
    # taller than the garment's own extent — while the unmasked one spans the
    # whole figure.
    top_height = top.bbox[3] - top.bbox[1]
    assert masked.height <= top_height + 2, (
        f"masked cutout is {masked.height}px tall but the top's bbox is "
        f"{top_height}px — the mask was not applied"
    )
    assert unmasked.height > masked.height


def test_masked_matte_output_is_still_rgba(seg_model) -> None:
    from stylist_ml import matting
    from stylist_ml.segmentation import segment

    if not matting.model_available():
        pytest.skip("u2net weights absent")

    image = worn_figure()
    masks = segment(seg_model, image).masks
    if not masks:
        pytest.skip("no masks in the fixture")

    result = matting.matte(image, mask_png=masks[0].mask_png)
    with Image.open(io.BytesIO(result.cutout_png)) as img:
        assert img.mode == "RGBA"


def test_a_mismatched_mask_size_is_resized_rather_than_crashing() -> None:
    """Defensive: a caller pairing a mask with a differently-sized image is a
    bug, but failing the whole ingest over it is worse than resizing."""
    from stylist_ml import matting

    if not matting.model_available():
        pytest.skip("u2net weights absent")

    small = Image.new("1", (50, 50), 1)
    buf = io.BytesIO()
    small.save(buf, format="PNG")
    result = matting.matte(flat_lay(), mask_png=buf.getvalue())
    assert result.width > 0
