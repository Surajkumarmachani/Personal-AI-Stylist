"""NSFW classification for the moderate gate (Step 4.4).

WHY THIS MODEL RUNS HERE AND NOT AT A PROVIDER
----------------------------------------------
A hosted moderation API would mean uploading the exact images we are trying to
keep off other people's infrastructure. The moderate stage sits before every
third-party call precisely so a flagged upload never leaves the VPC (View 1,
View 5), and satisfying it with a remote call would invert the guarantee.

TWO THRESHOLDS, NOT ONE
-----------------------
A single cutoff forces a choice between quarantining innocent photos and
letting through what it should catch. Two gives a middle band:

  >= QUARANTINE   terminal, audited, no third-party call ever made
  >= REVIEW       processed but flagged, visible to the user as needing review
  below           normal

The band matters because the base rate here is very low and the cost of a false
positive is high: telling someone their photo of a jumper was quarantined is a
worse product failure than asking them to confirm it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from stylist_ml.preprocess import load_rgb, to_tensor
from stylist_ml.runtime import LoadedModel

logger = logging.getLogger(__name__)

# PROVISIONAL: retune in P9 against real correction data. Deliberately high,
# because a false quarantine is a worse failure than a false pass into review.
QUARANTINE_THRESHOLD = 0.90
REVIEW_THRESHOLD = 0.60


@dataclass(frozen=True, slots=True)
class ModerationResult:
    nsfw_score: float
    verdict: str  # "pass" | "review" | "quarantine"
    model: str

    @property
    def is_quarantined(self) -> bool:
        return self.verdict == "quarantine"


def _softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - logits.max()
    exp = np.exp(shifted)
    return np.asarray(exp / exp.sum(), dtype=np.float32)


def moderate(model: LoadedModel, image_bytes: bytes) -> ModerationResult:
    img = load_rgb(image_bytes)
    assert model.spec.preprocess is not None
    tensor = to_tensor(img, model.spec.preprocess)

    logits = np.asarray(model.session.run(None, {model.input_name: tensor})[0], dtype=np.float32)
    probs = _softmax(logits.reshape(-1))

    # id2label is {0: sfw, 1: nsfw} — index, not label order, so this is read
    # from the registry rather than assumed.
    labels = model.spec.labels or ("sfw", "nsfw")
    nsfw_index = labels.index("nsfw")
    score = float(probs[nsfw_index])

    if score >= QUARANTINE_THRESHOLD:
        verdict = "quarantine"
    elif score >= REVIEW_THRESHOLD:
        verdict = "review"
    else:
        verdict = "pass"

    logger.info("moderated: nsfw=%.4f verdict=%s", score, verdict)
    return ModerationResult(nsfw_score=score, verdict=verdict, model=model.spec.name)
