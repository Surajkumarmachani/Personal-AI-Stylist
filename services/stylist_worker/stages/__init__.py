"""Ingest pipeline stages.

PHASE 4 SCOPE: validate, sanitise, moderate, segment, matte, classify, tag,
embed, persist. Dedupe (perceptual hash + cosine > 0.95) is Phase 5 and is
absent rather than stubbed.

TAG runs after CLASSIFY so the VLM prompt can carry what local extraction
already determined (slot, colour) — anchoring the model and ensuring a
cheaper, more reliable answer can never be overwritten by a costlier one.

Order matters, and two parts of it are requirements rather than preferences:

  - MODERATE runs before anything that could send pixels off our
    infrastructure, so a rejected upload never leaves the VPC (View 1, View 5).
  - SEGMENT runs before MATTE, because matting needs to know which garment it
    is being asked about. Without a mask, u2net mattes the whole frame and a
    mirror selfie's "shirt" comes back as the entire outfit.
"""

from stylist_worker.stages.classify import classify_stage
from stylist_worker.stages.embed import embed_stage
from stylist_worker.stages.matte import matte_stage
from stylist_worker.stages.moderate import moderate_stage
from stylist_worker.stages.persist import persist_stage
from stylist_worker.stages.sanitise import sanitise_stage
from stylist_worker.stages.segment import segment_stage
from stylist_worker.stages.tag import tag_stage
from stylist_worker.stages.validate import validate_stage

INGEST_STAGES = (
    validate_stage,
    sanitise_stage,
    moderate_stage,
    segment_stage,
    matte_stage,
    classify_stage,
    tag_stage,
    embed_stage,
    persist_stage,
)

__all__ = [
    "INGEST_STAGES",
    "classify_stage",
    "embed_stage",
    "matte_stage",
    "moderate_stage",
    "persist_stage",
    "sanitise_stage",
    "segment_stage",
    "tag_stage",
    "validate_stage",
]
