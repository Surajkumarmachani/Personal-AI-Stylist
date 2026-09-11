"""Perceptual hashing for near-duplicate detection (Phase 5).

WHY A PERCEPTUAL HASH AND AN EMBEDDING, NOT EITHER ALONE
--------------------------------------------------------
They fail in opposite directions, which is what makes the pair useful.

A perceptual hash answers "is this the same PICTURE?". It is near-exact: the
same photo re-uploaded, or re-encoded, or lightly resized, hashes the same.
It says nothing about whether two different photos show the same shirt.

An embedding answers "is this the same KIND of thing?". Two different white
oxford shirts photographed on the same hanger sit very close together — close
enough that a cosine threshold alone would keep proposing that a user's two
genuinely different shirts are one shirt.

So: phash catches re-uploads, cosine catches re-photographs, and requiring a
signal from either while never auto-merging keeps the false positives cheap —
a question the user answers, not a silent deletion.

dHash rather than aHash or pHash-DCT:
  - aHash (mean threshold) flips wholesale under a brightness shift, which is
    exactly what happens when the same garment is shot twice by a phone with
    auto-exposure.
  - DCT-based pHash is more robust still, but needs a DCT and a magic
    coefficient window; dHash gets most of the robustness from one idea —
    encode the SIGN of adjacent-pixel differences, which survives any
    monotonic brightness change — in a form that is auditable at a glance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL.Image import Image

# 8x8 comparisons => 64 bits. Small enough that near-duplicates survive
# re-encoding, large enough that unrelated garments practically never collide.
HASH_SIDE = 8

# Hamming distance at or below this is "the same picture". 64-bit dHash on
# re-encoded or mildly resized images typically differs by 0-4 bits; genuinely
# different photographs of the same garment run 20+. 10 sits in the empty
# space between, biased low because a false "duplicate" costs the user an
# interruption while a miss costs only a duplicate row they can merge later.
PHASH_DUPLICATE_BITS = 10

# Cosine similarity above this on the 768-d garment embedding is "probably the
# same garment photographed again". The plan specifies 0.95. Deliberately high:
# FashionSigLIP puts any two white shirts around 0.90, so a lower bar would
# propose merges between things the user knows are different, and a proposal
# the user keeps rejecting is worse than no proposal at all.
EMBEDDING_DUPLICATE_COSINE = 0.95


def dhash(image: Image) -> str:
    """64-bit difference hash, as 16 lowercase hex characters.

    Greyscale, resize to 9x8, then compare each pixel with its right-hand
    neighbour: 8 comparisons on each of 8 rows. The bit records only whether
    the left pixel is brighter, so any transform that preserves relative
    brightness — exposure, gamma, global contrast — preserves the hash.
    """
    # ANTIALIAS/LANCZOS downsampling, so a resized copy of an image reduces to
    # near-identical pixels rather than to whatever happened to land on the
    # sample points.
    small = image.convert("L").resize((HASH_SIDE + 1, HASH_SIDE), _resample())
    # tobytes(), not getdata(): in mode "L" this is exactly one byte per pixel
    # in row order, it avoids materialising a Python list per hash, and it
    # sidesteps the getdata()/get_flattened_data() deprecation churn — the
    # former is removed in Pillow 14, the latter does not exist in older
    # Pillow, so naming either one pins the version this can run against.
    pixels = small.tobytes()

    bits = 0
    for row in range(HASH_SIDE):
        base = row * (HASH_SIDE + 1)
        for col in range(HASH_SIDE):
            left = pixels[base + col]
            right = pixels[base + col + 1]
            bits = (bits << 1) | int(left > right)
    return f"{bits:016x}"


def _resample() -> int:
    """Pillow moved the resampling enum; support both without a version pin."""
    from PIL import Image as PILImage

    return int(getattr(PILImage, "Resampling", PILImage).LANCZOS)


def hamming(a: str, b: str) -> int:
    """Bit distance between two hex hashes.

    Raises on a length mismatch rather than comparing what it can: two hashes
    of different widths are not comparable, and silently returning a large
    distance would read as "not a duplicate" — the wrong answer produced
    confidently.
    """
    if len(a) != len(b):
        raise ValueError(f"hash width mismatch: {len(a)} vs {len(b)}")
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def is_near_duplicate_hash(
    a: str | None, b: str | None, *, bits: int = PHASH_DUPLICATE_BITS
) -> bool:
    """True when two phashes are close enough to be the same picture.

    A missing hash is NOT a duplicate. An unhashed garment is unknown, and
    treating unknown as a match would propose merges for every garment whose
    hashing failed.
    """
    if not a or not b:
        return False
    return hamming(a, b) <= bits
