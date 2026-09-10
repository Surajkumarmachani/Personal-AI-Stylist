"""Model registry: what we run, pinned to exact revisions.

WHY THIS IS A FETCH AND NOT AN EXPORT
-------------------------------------
Step 3.1 says to export SegFormer and FashionSigLIP to ONNX with a one-off
`scripts/export_models.py`. Both upstream repos already publish official ONNX
exports, so we download those instead, and that is the better trade:

  - no torch. An export path needs ~2GB of PyTorch toolchain that would exist
    solely to produce a file someone has already produced.
  - no drift. Our export settings (opset, dynamic axes, fusions) would be a
    second source of truth that can silently disagree with the published
    weights everyone else benchmarks against.
  - pinned. Each entry names a commit sha, so a fetch is reproducible even if
    the repo's main branch moves.

An export script becomes necessary the moment we FINE-TUNE — which is option
(c) of the Phase 3 ethnic-wear decision. If that happens, `export_models.py`
gets written then, and these entries point at our own artefact store instead.

CHECKSUMS ARE RECORDED, NOT ASSERTED, ON THE FIRST FETCH
--------------------------------------------------------
`sha256` starts as None for a model whose digest we have not yet observed.
The fetch script prints the digest it saw so it can be pasted in here, and from
then on a mismatch is a hard failure. Pinning by revision already prevents the
content changing under us; the checksum is defence against a corrupted download
or a tampered mirror, and §D1 requires it in the registry.

Long term this table lives in Postgres (`system_config`) and is served per-job,
so swapping a model is an UPDATE rather than a deploy. It is a Python literal
today because nothing yet needs to swap one at runtime; the shape is the same.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

MODELS_ROOT = Path(os.environ.get("MODELS_ROOT", "/models"))


@dataclass(frozen=True, slots=True)
class Preprocess:
    """Exactly what the model was trained to receive.

    These values are copied from each repo's preprocessor_config.json. Getting
    them wrong does not error — it silently degrades accuracy, which is the
    worst failure mode available, so they are pinned next to the weights rather
    than hardcoded at the call site.
    """

    size: tuple[int, int]  # (height, width)
    mean: tuple[float, float, float]
    std: tuple[float, float, float]
    # PIL resample filter. SegFormer's config says 2 (BILINEAR), SigLIP's says
    # 3 (BICUBIC) — they are genuinely different and not interchangeable.
    resample: int


@dataclass(frozen=True, slots=True)
class ModelSpec:
    name: str
    task: str
    repo: str
    revision: str  # commit sha, never a branch
    remote_file: str
    local_name: str
    preprocess: Preprocess | None = None
    sha256: str | None = None  # None until first observed; see module docstring
    notes: str = ""
    labels: tuple[str, ...] = field(default_factory=tuple)
    # Search for the file by name instead of trusting local_name. Needed for
    # u2net, whose download is done by rembg — it nests its own
    # models/<name>/ beneath the cache root, and that layout has changed
    # between rembg releases. Globbing means an upstream reshuffle does not
    # present as "model missing".
    glob_fallback: bool = False

    @property
    def path(self) -> Path:
        direct = MODELS_ROOT / self.local_name
        if direct.is_file() or not self.glob_fallback:
            return direct
        root = MODELS_ROOT / Path(self.local_name).parts[0]
        for found in sorted(root.rglob(Path(self.local_name).name)):
            return found
        return direct

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{self.repo}/resolve/{self.revision}/{self.remote_file}"

    def available(self) -> bool:
        return self.path.is_file()


# The 18 ATR classes, in label-id order, straight from the model's config.json.
#
# Index IS the class id the model emits, so order is load-bearing — do not sort
# this. `tests/test_ml_registry.py` asserts these against taxonomy.yaml's
# atr_to_slot keys, so the mapping cannot drift from the model that produces it.
ATR_LABELS: tuple[str, ...] = (
    "Background",
    "Hat",
    "Hair",
    "Sunglasses",
    "Upper-clothes",
    "Skirt",
    "Pants",
    "Dress",
    "Belt",
    "Left-shoe",
    "Right-shoe",
    "Face",
    "Left-leg",
    "Right-leg",
    "Left-arm",
    "Right-arm",
    "Bag",
    "Scarf",
)

SEGFORMER = ModelSpec(
    name="segformer_b2_clothes",
    task="segment",
    repo="mattmdjaga/segformer_b2_clothes",
    revision="584abc1e1d260e23c0fc627c5217a09b2b461046",
    remote_file="onnx/model.onnx",
    local_name="segformer_b2_clothes/model.onnx",
    preprocess=Preprocess(
        size=(512, 512),
        mean=(0.485, 0.456, 0.406),
        std=(0.229, 0.224, 0.225),
        resample=2,  # PIL.Image.BILINEAR
    ),
    sha256="a93a8dac171b5c1fcc53632a8bfc180bfd9759ea69a3e207451bb07f76add54f",
    labels=ATR_LABELS,
    notes=(
        "ATR human-parsing, 18 classes, none of which is a saree. The Phase 3 "
        "exit criterion is a measured number on the 50 ethnic-wear golden "
        "images; expect this entry to change once that verdict exists."
    ),
)

FASHION_SIGLIP = ModelSpec(
    name="marqo_fashionsiglip_vision",
    task="embed",
    repo="Marqo/marqo-fashionSigLIP",
    revision="c56244cc94f92419e8369fa71efdaf403b124ce8",
    # The VISION tower only. The text tower is for text->garment search, which
    # nothing needs yet; downloading it would be ~350MB of unused weights in
    # every ml pod.
    remote_file="onnx/vision_model.onnx",
    local_name="marqo_fashionsiglip/vision_model.onnx",
    preprocess=Preprocess(
        size=(224, 224),
        mean=(0.5, 0.5, 0.5),
        std=(0.5, 0.5, 0.5),
        resample=3,  # PIL.Image.BICUBIC
    ),
    sha256="a7e773846b27a699c45ba7e3978514b7fca420662d7e69e3b9226982f09f4a13",
    notes=(
        "fp32. Quantized variants (int8, fp16) exist upstream and are a P9 "
        "latency lever, but a quantized embedding model shifts every vector "
        "slightly — swapping one means re-embedding the whole corpus, so it is "
        "an extractor_version bump and a backfill, not a config change."
    ),
)

# u2net is fetched by rembg rather than by URL: it resolves its own cache from
# U2NET_HOME and handles the download. Listed here so /models reports one
# complete inventory instead of two half-inventories.
U2NET = ModelSpec(
    name="u2net",
    task="matte",
    repo="danielgatis/rembg",
    revision="v0.0.0",
    remote_file="u2net.onnx",
    local_name="u2net/u2net.onnx",
    glob_fallback=True,
    sha256="8d10d2f3bb75ae3b6d527c77944fc5e7dcd94b29809d47a739a7a728a912b491",
    notes="Fetched via rembg (U2NET_HOME), which nests its own models/<name>/ path.",
)

NSFW = ModelSpec(
    name="vit_nsfw_detector",
    task="moderate",
    repo="AdamCodd/vit-base-nsfw-detector",
    revision="8587de998f441aac03fdd57a85d2e4cb808c7d64",
    remote_file="onnx/model.onnx",
    local_name="vit_nsfw_detector/model.onnx",
    preprocess=Preprocess(
        size=(384, 384),
        mean=(0.5, 0.5, 0.5),
        std=(0.5, 0.5, 0.5),
        resample=3,  # PIL.Image.BICUBIC
    ),
    sha256="dce8f5af8509fee39c453b78a66076ead5c97321ddcee0ddfa16f67dc8286384",
    labels=("sfw", "nsfw"),
    notes=(
        "Runs IN-VPC and BEFORE any third-party call, which is the whole point "
        "of the moderate stage's position in the pipeline: a flagged upload "
        "never leaves our infrastructure (View 1, View 5). Using a hosted "
        "moderation API here would send the very images we are trying not to "
        "send anywhere."
    ),
)

ALL_MODELS: tuple[ModelSpec, ...] = (U2NET, SEGFORMER, FASHION_SIGLIP, NSFW)

EMBEDDING_DIM = 768  # must match the pgvector column width in Phase 3.4


def by_name(name: str) -> ModelSpec:
    for spec in ALL_MODELS:
        if spec.name == name:
            return spec
    raise KeyError(f"unknown model {name!r}; known: {[m.name for m in ALL_MODELS]}")
