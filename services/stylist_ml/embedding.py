"""Garment embeddings from Marqo-FashionSigLIP (vision tower).

768 dimensions, L2-normalised. Both of those are contracts, not details:

  - 768 must match the pgvector column width declared in Step 3.4. A model
    swap that changes the dimension is a migration, not a config change.
  - L2 normalisation is what makes cosine distance meaningful. pgvector's
    `vector_cosine_ops` normalises internally for the distance computation,
    but the raw vectors are also used for the style-vector EWMA in Phase 8,
    where averaging un-normalised vectors would weight long vectors more
    heavily for no reason anyone intended.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from stylist_ml.preprocess import load_rgb, to_tensor
from stylist_ml.registry import EMBEDDING_DIM
from stylist_ml.runtime import LoadedModel

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EmbedResult:
    vector: tuple[float, ...]
    dim: int
    model: str


def embed(model: LoadedModel, image_bytes: bytes) -> EmbedResult:
    img = load_rgb(image_bytes)
    assert model.spec.preprocess is not None
    tensor = to_tensor(img, model.spec.preprocess)

    outputs = model.session.run(None, {model.input_name: tensor})
    raw = np.asarray(outputs[0], dtype=np.float32)

    # The vision tower emits [1, 768]. Some ONNX exports of pooled models emit
    # [1, tokens, dim] instead; mean-pool that case rather than silently
    # embedding whatever the first token happens to be.
    if raw.ndim == 3:
        raw = raw.mean(axis=1)
    vector = raw.reshape(-1)

    if vector.shape[0] != EMBEDDING_DIM:
        raise ValueError(
            f"{model.spec.name} produced {vector.shape[0]} dimensions, "
            f"expected {EMBEDDING_DIM} — the pgvector column width and this "
            "model disagree, which would corrupt every stored vector"
        )

    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        # A zero vector has no direction, so cosine similarity against it is
        # undefined and would quietly rank as "maximally similar to nothing".
        raise ValueError(f"{model.spec.name} produced a zero vector")
    vector = vector / norm

    return EmbedResult(
        vector=tuple(float(v) for v in vector),
        dim=int(vector.shape[0]),
        model=model.spec.name,
    )
