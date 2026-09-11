"""Outfit suggestion pipeline (Phase 6). See pipeline.py."""

from stylist_suggest.pipeline import (
    CandidatePool,
    SuggestionResult,
    garment_set_hash,
    generate_candidates,
    load_wardrobe,
    suggest,
)

__all__ = [
    "CandidatePool",
    "SuggestionResult",
    "garment_set_hash",
    "generate_candidates",
    "load_wardrobe",
    "suggest",
]
