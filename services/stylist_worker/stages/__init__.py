"""Ingest pipeline stages.

PHASE 2 SCOPE: validate, sanitise, moderate (stub), matte, persist.
Segmentation, classification, VLM tagging, embedding and dedupe are Phases 3-5
and are deliberately absent rather than stubbed.

Order matters and one part of it is a policy requirement, not a preference:
moderation runs BEFORE anything that could send pixels off our infrastructure,
so a rejected upload never leaves the VPC (View 1 / View 5).
"""

from stylist_worker.stages.matte import matte_stage
from stylist_worker.stages.moderate import moderate_stage
from stylist_worker.stages.persist import persist_stage
from stylist_worker.stages.sanitise import sanitise_stage
from stylist_worker.stages.validate import validate_stage

# The Phase 2 pipeline, in execution order.
INGEST_STAGES = (
    validate_stage,
    sanitise_stage,
    moderate_stage,
    matte_stage,
    persist_stage,
)

__all__ = [
    "INGEST_STAGES",
    "matte_stage",
    "moderate_stage",
    "persist_stage",
    "sanitise_stage",
    "validate_stage",
]
