"""Style vector: EWMA over the embeddings of outfits you reacted to (Phase 8).

ONE FUNCTION, TWO CALLERS, AND THAT IS THE WHOLE POINT
------------------------------------------------------
The live update (a user taps "worn") and `scripts/rebuild_style_vectors.py`
(replays the event log) both call `apply_event`. They are not two
implementations that must agree — they are one implementation called twice.

The plan's warning is blunt: write the rebuild script in this phase "or you
will accumulate unrebuildable state", and the exit criterion asserts the replay
reproduces the live vector exactly. Equality is only achievable if the update
is a PURE FUNCTION of (previous vector, event) applied in a defined order, so
that is what this module is. No clock, no database, no randomness.

WHY EWMA AND NOT AN AVERAGE
---------------------------
An average treats what you wore two years ago as equal evidence to this
morning. Taste moves; a wardrobe app that cannot follow it will keep
recommending the person you used to be. alpha ~= 0.1 means roughly the last ~20
reactions dominate, which is a few weeks of ordinary use — slow enough that one
odd Tuesday does not redefine you, fast enough to track a season.

DISLIKE PUSHES AWAY, IT DOES NOT ERASE
--------------------------------------
A dislike moves the vector AWAY from that outfit rather than skipping it.
Ignoring dislikes throws away half the signal — and the informative half, since
people dislike more precisely than they like. But it is DAMPED (`alpha/2`): a user
rejecting an outfit is saying "not today", and a single strong negative should
not be able to invert a taste built from twenty positives.

NORMALISED AFTER EVERY STEP
---------------------------
These are cosine-space vectors and only direction carries meaning. Without
renormalising, repeated negatives shrink the magnitude toward zero and the
cosine against every garment converges on the same value — a style vector that
silently stops discriminating while still looking like a vector.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# ~20 reactions of effective memory.
# PROVISIONAL — re-dated 2026-09-17: P9 arrived with no real traffic.
# Resolves when: ~100 real feedback events, enough to see whether the vector
# tracks taste or chases noise. The plan says alpha ~= 0.1 and there is still
# no data to argue otherwise.
DEFAULT_ALPHA = 0.1

# Kinds that move the vector, and which way. `saved` is treated as a weak
# positive: saving is intent, not endorsement. `dismissed` is deliberately
# ABSENT — dismissing a card is usually "not now" or a mis-tap, and reading it
# as taste would let scrolling past something teach the system to avoid it.
POSITIVE = {"like": 1.0, "worn": 1.0, "saved": 0.5}
NEGATIVE = {"dislike": 1.0}


@dataclass(frozen=True)
class StyleVector:
    """A unit vector plus the audit trail that makes it rebuildable."""

    vector: tuple[float, ...]
    events_applied: int
    last_event_id: str | None
    alpha: float

    def cosine(self, other: list[float] | tuple[float, ...]) -> float:
        """Similarity against a garment embedding. Both are unit-length in the
        happy path, but `other` comes from the database and may not be, so this
        does not assume it."""
        if len(other) != len(self.vector):
            raise ValueError(f"dimension mismatch: {len(self.vector)} vs {len(other)}")
        dot = sum(a * b for a, b in zip(self.vector, other, strict=True))
        norm = math.sqrt(sum(b * b for b in other))
        return dot / norm if norm else 0.0


def normalise(vec: list[float] | tuple[float, ...]) -> tuple[float, ...]:
    """Scale to unit length. A zero vector stays zero rather than becoming NaN.

    A zero vector is a real state — it is what a user with no feedback has, and
    what a perfectly balanced set of likes and dislikes converges to. Dividing
    by its norm would poison every downstream score with NaN, and NaN compares
    False against every threshold, so the failure would present as "this user
    gets no suggestions" rather than as an arithmetic error.
    """
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return tuple(0.0 for _ in vec)
    return tuple(v / norm for v in vec)


def parse_embedding(raw: object) -> list[float] | None:
    """Coerce whatever the driver hands back into a list of floats.

    pgvector's `vector` type arrives as a STRING (`'[0.1,0.2,...]'`) when the
    column is read through raw SQL, because the type adapter is registered for
    the ORM mapping and not for `text()` queries. Until Phase 8 nothing ever
    pulled an embedding into Python — every use was `embedding <=> ...` inside
    SQL, where the database does the work — so this only surfaced when the
    style vector needed the actual numbers, as a `TypeError: unsupported
    operand type(s) for +: 'int' and 'str'` three frames from the query.

    Accepts the list form too, so a caller that DOES go through the ORM, or a
    test that passes literals, does not need to know which path it is on.
    """
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        return [float(v) for v in raw]
    if isinstance(raw, str):
        stripped = raw.strip().strip("[]")
        if not stripped:
            return None
        try:
            return [float(part) for part in stripped.split(",")]
        except ValueError:
            return None
    return None


def outfit_embedding(garment_embeddings: list[list[float]]) -> tuple[float, ...] | None:
    """One vector for a whole outfit: the normalised mean of its garments.

    A mean, not a concatenation or a max. An outfit IS its combination, and
    averaging is the only operation that does not depend on slot order — a
    concatenation would make [shirt, trousers] and [trousers, shirt] different
    outfits, and outfits are sets.

    Returns None when nothing usable came back, rather than a zero vector,
    because "this outfit has no embedding" and "this outfit sits at the origin"
    must not be the same value to the caller.
    """
    usable = [e for e in garment_embeddings if e]
    if not usable:
        return None
    dim = len(usable[0])
    if any(len(e) != dim for e in usable):
        raise ValueError("garment embeddings have inconsistent dimensions")
    mean = [sum(e[i] for e in usable) / len(usable) for i in range(dim)]
    return normalise(mean)


def apply_event(
    current: StyleVector | None,
    *,
    kind: str,
    outfit_vec: tuple[float, ...],
    event_id: str,
    alpha: float = DEFAULT_ALPHA,
) -> StyleVector:
    """Fold one feedback event into the style vector. Pure.

    Called by the live handler and by the rebuild script, in the same order,
    so the two cannot drift. Events whose kind carries no taste signal
    (`dismissed`) return the current vector UNCHANGED but still advance the
    counter — the rebuild must account for every row it read, or "events
    applied" stops being a number the two sides can compare.
    """
    weight = POSITIVE.get(kind)
    sign = 1.0
    if weight is None:
        weight = NEGATIVE.get(kind)
        # A dislike is damped. Someone rejecting one outfit is saying "not
        # today"; it must not outweigh twenty positives.
        sign = -1.0
        if weight is not None:
            weight *= 0.5

    base = current.vector if current is not None else tuple(0.0 for _ in outfit_vec)
    if len(base) != len(outfit_vec):
        raise ValueError(f"dimension mismatch: {len(base)} vs {len(outfit_vec)}")

    if weight is None:
        # No taste signal. Counted, not applied.
        updated = base
    else:
        step = alpha * weight
        updated = normalise(
            tuple(b * (1.0 - step) + sign * step * o for b, o in zip(base, outfit_vec, strict=True))
        )

    return StyleVector(
        vector=updated,
        events_applied=(current.events_applied if current else 0) + 1,
        last_event_id=event_id,
        alpha=alpha,
    )
