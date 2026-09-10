"""Colour extraction against the taxonomy palette.

Pure functions over arrays. No db, no api, no clients — `stylist_domain` stays
importable without infrastructure, which is what lets the scoring path be
tested without a database.

WHY CIELAB AND DELTA-E 2000 RATHER THAN RGB DISTANCE
----------------------------------------------------
taxonomy.yaml says the hex anchors are "load-bearing, not decorative" and that
the scorer computes ΔE2000 against them. That is not ceremony:

  - RGB distance is perceptually wrong. #000000 to #0000FF measures as far as
    #00FF00 to #00FFFF, but one pair looks like two different colours and the
    other like two shades of cyan.
  - The palette has nine values that exist specifically for ethnic wear
    (maroon, rust, rani_pink, mustard, emerald, teal, gold, silver,
    cream_ivory). Several are close together in RGB and clearly distinct to a
    person — maroon vs red, rust vs orange, cream vs white. Euclidean RGB
    collapses exactly the distinctions the palette was extended to make.
  - ΔE2000 over CIE76 because CIE76 systematically overestimates differences in
    saturated blues, and denim_indigo vs blue_navy is a real pair we have to
    tell apart.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# sRGB D65 -> XYZ. Standard matrix; not a tunable.
_RGB_TO_XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ],
    dtype=np.float64,
)
# D65 white point.
_WHITE = np.array([0.95047, 1.00000, 1.08883], dtype=np.float64)


def srgb_to_linear(rgb: np.ndarray) -> np.ndarray:
    """Undo the sRGB transfer function. Input and output in [0,1].

    Skipping this — treating 8-bit values as linear — is the single most common
    colour bug. It shifts every midtone and makes dark colours cluster.
    """
    rgb = np.asarray(rgb, dtype=np.float64)
    return np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)


def rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """(..., 3) uint8 or float [0,255] sRGB -> (..., 3) CIELAB."""
    arr = np.asarray(rgb, dtype=np.float64)
    if arr.max(initial=0.0) > 1.0:
        arr = arr / 255.0
    linear = srgb_to_linear(arr)
    xyz = linear @ _RGB_TO_XYZ.T / _WHITE

    epsilon = 216 / 24389
    kappa = 24389 / 27
    f = np.where(xyz > epsilon, np.cbrt(xyz), (kappa * xyz + 16) / 116)

    lab = np.empty_like(f)
    lab[..., 0] = 116 * f[..., 1] - 16
    lab[..., 1] = 500 * (f[..., 0] - f[..., 1])
    lab[..., 2] = 200 * (f[..., 1] - f[..., 2])
    return lab


def hex_to_lab(hex_colour: str) -> np.ndarray:
    h = hex_colour.lstrip("#")
    rgb = np.array([int(h[i : i + 2], 16) for i in (0, 2, 4)], dtype=np.float64)
    return rgb_to_lab(rgb)


def delta_e_2000(lab1: np.ndarray, lab2: np.ndarray) -> float:
    """CIEDE2000 difference between two Lab colours.

    The full formula, including the hue-rotation term. Truncated
    implementations that drop R_T are wrong precisely in the blues, which is
    where denim_indigo and blue_navy live.
    """
    l1, a1, b1 = (float(v) for v in lab1)
    l2, a2, b2 = (float(v) for v in lab2)

    c1 = math.hypot(a1, b1)
    c2 = math.hypot(a2, b2)
    c_bar = (c1 + c2) / 2

    g = 0.5 * (1 - math.sqrt(c_bar**7 / (c_bar**7 + 25**7))) if c_bar > 0 else 0.0
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)

    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if (a1p or b1) else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if (a2p or b2) else 0.0

    dlp = l2 - l1
    dcp = c2p - c1p
    if c1p * c2p == 0:
        dhp = 0.0
    elif abs(h2p - h1p) <= 180:
        dhp = h2p - h1p
    elif h2p - h1p > 180:
        dhp = h2p - h1p - 360
    else:
        dhp = h2p - h1p + 360

    # from ΔH' (dHp, a chroma-weighted difference). Renaming either to
    # satisfy a casing rule loses the correspondence to the published
    # formula, which is the only way to check this implementation.
    dHp = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp) / 2)  # noqa: N806

    lp_bar = (l1 + l2) / 2
    cp_bar = (c1p + c2p) / 2
    if c1p * c2p == 0:
        hp_bar = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hp_bar = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hp_bar = (h1p + h2p + 360) / 2
    else:
        hp_bar = (h1p + h2p - 360) / 2

    t = (
        1
        - 0.17 * math.cos(math.radians(hp_bar - 30))
        + 0.24 * math.cos(math.radians(2 * hp_bar))
        + 0.32 * math.cos(math.radians(3 * hp_bar + 6))
        - 0.20 * math.cos(math.radians(4 * hp_bar - 63))
    )
    d_theta = 30 * math.exp(-(((hp_bar - 275) / 25) ** 2))
    r_c = 2 * math.sqrt(cp_bar**7 / (cp_bar**7 + 25**7)) if cp_bar > 0 else 0.0
    s_l = 1 + (0.015 * (lp_bar - 50) ** 2) / math.sqrt(20 + (lp_bar - 50) ** 2)
    s_c = 1 + 0.045 * cp_bar
    s_h = 1 + 0.015 * cp_bar * t
    r_t = -math.sin(math.radians(2 * d_theta)) * r_c

    return math.sqrt(
        (dlp / s_l) ** 2 + (dcp / s_c) ** 2 + (dHp / s_h) ** 2 + r_t * (dcp / s_c) * (dHp / s_h)
    )


@dataclass(frozen=True, slots=True)
class ColourReading:
    primary: str
    primary_share: float
    primary_delta_e: float
    secondary: str | None
    secondary_share: float | None
    confidence: float
    # True when the garment is genuinely multi-hue rather than two-tone. The
    # taxonomy has `multicolour` as an explicit escape valve for prints and
    # bandhani, which is not the same as "we could not decide".
    is_multicolour: bool


# A second hue counts as `secondary_colour` only above this share of the
# garment. GOLDEN_SET_SPEC pins the same 15% for human labellers, so the model
# and the ground truth are answering the same question.
SECONDARY_MIN_SHARE = 0.15

# Above this many distinct palette colours with meaningful share, the garment
# is a print rather than a two-tone item.
MULTICOLOUR_MIN_HUES = 4

# ΔE2000 above which even the closest palette anchor is a poor match, so the
# reading is flagged for review rather than asserted. ~10 is "clearly a
# different colour" to a person; the palette is dense enough that a real
# garment colour should land well inside it.
POOR_MATCH_DELTA_E = 22.0


def kmeans(
    pixels: np.ndarray, k: int, *, iterations: int = 25, seed: int = 0
) -> tuple[np.ndarray, np.ndarray]:
    """Minimal k-means in Lab space. Returns (centroids, counts).

    Hand-rolled rather than pulling in scikit-learn: this is ~15 lines against
    a ~30MB dependency in the worker image, and k-means on a few thousand
    subsampled pixels needs none of what sklearn adds.

    Seeded deterministically. Non-deterministic colour extraction would mean a
    garment's colour changes when a backfill re-runs, which looks like data
    corruption to a user who corrected it once already.
    """
    rng = np.random.default_rng(seed)
    if len(pixels) <= k:
        return pixels.copy(), np.ones(len(pixels), dtype=np.int64)

    # k-means++ style seeding, cheaply: first centre at random, the rest biased
    # toward being far from what is already chosen. Plain random seeding
    # regularly puts two centres in the same cluster and leaves a real colour
    # unrepresented.
    centroids = [pixels[rng.integers(len(pixels))]]
    for _ in range(k - 1):
        d = np.min(np.stack([np.sum((pixels - c) ** 2, axis=1) for c in centroids]), axis=0)
        total = d.sum()
        if total <= 0:
            centroids.append(pixels[rng.integers(len(pixels))])
            continue
        centroids.append(pixels[rng.choice(len(pixels), p=d / total)])
    centres = np.stack(centroids)

    labels = np.zeros(len(pixels), dtype=np.int64)
    for _ in range(iterations):
        distances = np.sum((pixels[:, None, :] - centres[None, :, :]) ** 2, axis=2)
        new_labels = np.argmin(distances, axis=1)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for i in range(len(centres)):
            member = pixels[labels == i]
            if len(member):
                centres[i] = member.mean(axis=0)

    counts = np.bincount(labels, minlength=len(centres))
    return centres, counts


def read_colours(
    rgb_pixels: np.ndarray,
    palette: dict[str, str | None],
    *,
    k: int = 5,
    max_pixels: int = 20000,
    seed: int = 0,
) -> ColourReading:
    """Extract primary/secondary palette colours from a garment's pixels.

    `rgb_pixels` is (N, 3) — ALREADY filtered to the garment. Callers pass the
    non-transparent pixels of a cutout: including the background would make
    every garment on a white bed read as partly white.

    `palette` maps colour id -> hex, with None for `multicolour` (which has no
    single anchor and is assigned by rule, not by distance).
    """
    if len(rgb_pixels) == 0:
        raise ValueError("no pixels to read a colour from")

    anchors = {cid: hex_to_lab(h) for cid, h in palette.items() if h}
    if not anchors:
        raise ValueError("palette has no hex anchors")

    pixels = np.asarray(rgb_pixels, dtype=np.float64)
    if len(pixels) > max_pixels:
        # Subsample deterministically. Colour proportions are a property of the
        # distribution, not of every pixel, and 20k is far more than enough to
        # estimate them.
        rng = np.random.default_rng(seed)
        pixels = pixels[rng.choice(len(pixels), size=max_pixels, replace=False)]

    lab = rgb_to_lab(pixels)
    centres, counts = kmeans(lab, k, seed=seed)
    total = float(counts.sum())

    # Fold clusters onto palette anchors, accumulating share per palette id.
    # Two clusters can legitimately land on the same anchor (highlight and
    # shadow of one garment colour), and they must count as one colour rather
    # than as primary and secondary.
    share: dict[str, float] = {}
    best_delta: dict[str, float] = {}
    for centre, count in zip(centres, counts, strict=True):
        if count == 0:
            continue
        cid, delta = min(
            ((cid, delta_e_2000(centre, anchor)) for cid, anchor in anchors.items()),
            key=lambda pair: pair[1],
        )
        share[cid] = share.get(cid, 0.0) + count / total
        best_delta[cid] = min(best_delta.get(cid, math.inf), delta)

    ranked = sorted(share.items(), key=lambda kv: -kv[1])
    primary, primary_share = ranked[0]
    secondary, secondary_share = (None, None)
    if len(ranked) > 1 and ranked[1][1] >= SECONDARY_MIN_SHARE:
        secondary, secondary_share = ranked[1]

    meaningful_hues = sum(1 for _, s in ranked if s >= SECONDARY_MIN_SHARE / 2)
    is_multicolour = meaningful_hues >= MULTICOLOUR_MIN_HUES

    # Confidence blends "how dominant is the primary" with "how well does it
    # actually match an anchor". A 90%-dominant colour that is ΔE 30 from
    # everything in the palette is not a confident reading — it means the
    # palette is missing a value, which is a taxonomy bug worth surfacing.
    delta = best_delta[primary]
    match_quality = max(0.0, 1.0 - delta / POOR_MATCH_DELTA_E)
    confidence = round(min(1.0, 0.5 * primary_share + 0.5 * match_quality), 4)

    # Every field is coerced to a PYTHON primitive, not left as a numpy scalar.
    #
    # Shares are computed as `count / total` where count is an np.int64, so
    # they come out as np.float64 — and `np.float64 < float` yields np.bool_,
    # not bool. asyncpg rejects that with
    #   invalid input for query argument: np.False_ (a boolean is required)
    # which surfaces three layers away, as a DataError on an UPDATE, long
    # after the type actually escaped. Converting at the boundary keeps numpy
    # inside this module.
    return ColourReading(
        primary="multicolour" if is_multicolour else primary,
        primary_share=round(float(primary_share), 4),
        primary_delta_e=round(float(delta), 2),
        secondary=None if is_multicolour else secondary,
        secondary_share=round(float(secondary_share), 4) if secondary_share else None,
        confidence=float(confidence),
        is_multicolour=bool(is_multicolour),
    )
