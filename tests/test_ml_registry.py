"""Model registry and preprocessing.

These run without weights on disk, so they run everywhere. Inference itself is
tested in tests/test_ml_inference.py, which skips when the models are absent.

The load-bearing test here is
`test_atr_labels_match_the_taxonomys_mapping`: the model emits class ids whose
meaning comes from ATR_LABELS, and taxonomy.yaml maps those label names to
slots. If the two ever disagree — a renamed label, a reordered list — every
garment gets the wrong slot, silently, with no error anywhere. Nothing else in
the system would notice.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from stylist_domain.taxonomy import load_taxonomy
from stylist_ml import registry
from stylist_ml.preprocess import bbox_of, load_rgb, mask_to_png, to_tensor


def test_atr_labels_match_the_taxonomys_mapping() -> None:
    taxonomy = load_taxonomy()
    model_labels = set(registry.ATR_LABELS)
    taxonomy_labels = set(taxonomy.atr_to_slot)

    assert model_labels == taxonomy_labels, (
        "the model's class labels and taxonomy.yaml's atr_to_slot have drifted\n"
        f"  model only:    {sorted(model_labels - taxonomy_labels)}\n"
        f"  taxonomy only: {sorted(taxonomy_labels - model_labels)}"
    )


def test_atr_label_order_is_the_models_class_ids() -> None:
    """Index IS the class id, so the order cannot be sorted or rearranged.

    Taken from the model's own config.json id2label. If someone tidies this
    list alphabetically, every mask comes back labelled as a different garment.
    """
    assert registry.ATR_LABELS[0] == "Background"
    assert registry.ATR_LABELS[4] == "Upper-clothes"
    assert registry.ATR_LABELS[7] == "Dress"
    assert registry.ATR_LABELS[17] == "Scarf"
    assert len(registry.ATR_LABELS) == 18


def test_every_garment_class_maps_to_a_real_slot() -> None:
    taxonomy = load_taxonomy()
    slots = set(taxonomy.slots)
    for label in registry.ATR_LABELS:
        slot = taxonomy.slot_for_atr_class(label)
        if slot is not None:
            assert slot in slots, f"{label} maps to unknown slot {slot!r}"


def test_non_garment_classes_are_exactly_the_unmapped_ones() -> None:
    """The two definitions of "not a garment" must agree.

    segmentation.py hardcodes the body-part class ids for speed; taxonomy.yaml
    expresses the same thing as `atr_to_slot: null`. Divergence would mean
    either emitting a mask for someone's arm as a garment, or dropping a real
    garment class.
    """
    from stylist_ml.segmentation import NON_GARMENT_CLASSES

    taxonomy = load_taxonomy()
    unmapped = {
        i
        for i, label in enumerate(registry.ATR_LABELS)
        if taxonomy.slot_for_atr_class(label) is None
    }
    assert unmapped == NON_GARMENT_CLASSES, (
        f"segmentation.NON_GARMENT_CLASSES={sorted(NON_GARMENT_CLASSES)} "
        f"but taxonomy leaves {sorted(unmapped)} unmapped"
    )


def test_embedding_dim_matches_the_planned_pgvector_width() -> None:
    """768 is a schema contract, not a preference: Step 3.4 declares
    vector(768), and a model that emits a different width would corrupt every
    stored vector rather than fail loudly."""
    assert registry.EMBEDDING_DIM == 768


def test_models_are_pinned_to_commit_shas_not_branches() -> None:
    """A branch pin means the weights can change under you between two
    deploys of identical code."""
    for spec in (registry.SEGFORMER, registry.FASHION_SIGLIP):
        assert len(spec.revision) == 40, f"{spec.name} revision is not a full sha"
        assert spec.revision.isalnum()
        assert spec.revision not in ("main", "master")


def test_every_model_has_a_pinned_checksum() -> None:
    for spec in registry.ALL_MODELS:
        assert spec.sha256, f"{spec.name} has no pinned sha256"
        assert len(spec.sha256) == 64


def test_urls_are_https_and_carry_the_revision() -> None:
    for spec in (registry.SEGFORMER, registry.FASHION_SIGLIP):
        assert spec.url.startswith("https://huggingface.co/")
        assert spec.revision in spec.url


def test_by_name_rejects_unknown_models() -> None:
    assert registry.by_name("u2net").task == "matte"
    with pytest.raises(KeyError):
        registry.by_name("not-a-model")


# ------------------------------------------------------------- preprocessing


def _png(size: tuple[int, int], colour: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="PNG")
    return buf.getvalue()


def test_to_tensor_produces_the_shape_and_normalisation_onnx_expects() -> None:
    img = Image.new("RGB", (300, 400), (128, 128, 128))
    tensor = to_tensor(img, registry.SEGFORMER.preprocess)

    assert tensor.shape == (1, 3, 512, 512), "must be NCHW at the model's input size"
    assert tensor.dtype == np.float32

    # 128/255 = 0.502; normalised by ImageNet mean/std that lands near 0.2.
    expected = (128 / 255 - 0.485) / 0.229
    assert abs(float(tensor[0, 0, 0, 0]) - expected) < 0.01


def test_siglip_and_segformer_normalisation_genuinely_differ() -> None:
    """Guard against someone consolidating the two Preprocess entries.

    SigLIP was trained on [-1,1] (mean/std 0.5); SegFormer on ImageNet
    statistics. Using one for the other does not error — it silently degrades
    accuracy, which is why the constants live beside each model's weights.
    """
    img = Image.new("RGB", (256, 256), (200, 100, 50))
    seg = to_tensor(img, registry.SEGFORMER.preprocess)
    sig = to_tensor(img, registry.FASHION_SIGLIP.preprocess)
    assert seg.shape[-1] == 512
    assert sig.shape[-1] == 224
    assert abs(float(seg[0, 0, 0, 0]) - float(sig[0, 0, 0, 0])) > 0.1


def test_load_rgb_flattens_alpha_onto_white() -> None:
    """Cutouts are RGBA, and a 3-channel model cannot take 4 channels.

    White rather than black: a black fill reads as a dark garment edge to a
    model trained on photographs, and the alpha edge is exactly where
    segmentation and matting quality matter most.
    """
    buf = io.BytesIO()
    Image.new("RGBA", (64, 64), (255, 0, 0, 0)).save(buf, format="PNG")
    img = load_rgb(buf.getvalue())
    assert img.mode == "RGB"
    assert img.getpixel((0, 0)) == (255, 255, 255)


def test_load_rgb_accepts_plain_rgb_unchanged() -> None:
    img = load_rgb(_png((32, 32), (10, 20, 30)))
    assert img.mode == "RGB"
    assert img.getpixel((0, 0)) == (10, 20, 30)


def test_mask_to_png_round_trips_as_one_bit() -> None:
    mask = np.zeros((40, 40), dtype=bool)
    mask[10:30, 5:35] = True
    png = mask_to_png(mask)
    with Image.open(io.BytesIO(png)) as decoded:
        assert decoded.mode == "1", "8-bit greyscale would be 8x the bytes for one bit"
        restored = np.asarray(decoded, dtype=bool)
    assert restored.shape == mask.shape
    assert (restored == mask).all()


def test_bbox_of_is_tight_and_exclusive_at_the_far_edge() -> None:
    mask = np.zeros((100, 80), dtype=bool)
    mask[20:50, 10:30] = True
    assert bbox_of(mask) == (10, 20, 30, 50)


def test_bbox_of_empty_mask_is_none() -> None:
    assert bbox_of(np.zeros((10, 10), dtype=bool)) is None
