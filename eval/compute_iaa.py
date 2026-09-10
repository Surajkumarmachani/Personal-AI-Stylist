"""Inter-annotator agreement, per-field Cohen's kappa (GOLDEN_SET_SPEC §4).

    python eval/compute_iaa.py

THE STEP TEAMS SKIP, AND THE REASON IT MATTERS
----------------------------------------------
If two humans agree only 55% of the time on `formality`, you cannot hold a
model to 75% — and any eval floor on that field is measuring noise. The floor
would then fail builds for no reason, or pass them for no reason, and either
way the number means nothing.

GOLDEN_SET_SPEC predicts which fields will score badly: `material` (silk vs
satin vs crepe from a photo is genuinely hard), `formality` on smart-casual
items, `fit` on draped garments. The two legitimate responses are both
recorded, not argued: merge the confusable values in the taxonomy, or drop the
field's CI floor and treat it as advisory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERLAP_DIR = REPO_ROOT / "eval" / "golden" / "overlap"
FIELDS = (
    "slot",
    "subcategory",
    "primary_colour",
    "secondary_colour",
    "pattern",
    "material",
    "formality",
    "dress_code",
    "warmth",
    "fit",
)


def cohens_kappa(a: list[str], b: list[str]) -> float:
    """Agreement corrected for chance.

    Raw agreement is inflated whenever one value dominates: if 80% of garments
    are `solid`, two labellers who both guess `solid` every time agree 80% of
    the time while demonstrating no skill at all. Kappa subtracts that.
    """
    if not a:
        return 0.0
    categories = sorted(set(a) | set(b))
    n = len(a)
    observed = sum(1 for x, y in zip(a, b, strict=True) if x == y) / n
    expected = sum((a.count(c) / n) * (b.count(c) / n) for c in categories)
    if expected >= 1.0:
        return 1.0
    return (observed - expected) / (1 - expected)


def load(path: Path) -> dict[str, dict[str, str]]:
    """image_id#garment_index -> field values."""
    out: dict[str, dict[str, str]] = {}
    with path.open() as fh:
        for line in fh:
            if not line.strip():
                continue
            label = json.loads(line)
            for garment in label.get("garments", []):
                key = f"{label['image_id']}#{garment.get('garment_index', 0)}"
                out[key] = {f: str(garment.get(f)) for f in FIELDS}
    return out


def main() -> int:
    a_path = OVERLAP_DIR / "labeller_a.jsonl"
    b_path = OVERLAP_DIR / "labeller_b.jsonl"
    if not (a_path.is_file() and b_path.is_file()):
        print(f"overlap labels not found under {OVERLAP_DIR.relative_to(REPO_ROOT)}/")
        print("\nGOLDEN_SET_SPEC §4 requires a 50-image overlap labelled")
        print("INDEPENDENTLY by two labellers before either touches the other 450.")
        print("Without it, every accuracy floor in taxonomy.yaml is unvalidated:")
        print("a field humans cannot agree on cannot be a CI gate.")
        return 2

    a, b = load(a_path), load(b_path)
    shared = sorted(set(a) & set(b))
    if not shared:
        print("the two labellers scored no garments in common")
        return 1

    print(f"overlap: {len(shared)} garments\n")
    print(f"  {'field':18s} {'kappa':>7s}  reading")
    advisory: list[str] = []
    for fieldname in FIELDS:
        av = [a[k][fieldname] for k in shared]
        bv = [b[k][fieldname] for k in shared]
        k = cohens_kappa(av, bv)
        if k > 0.80:
            reading = "strong — proceed"
        elif k >= 0.60:
            reading = "moderate — tighten the guide, re-do the overlap"
        else:
            reading = "POOR — field is unmeasurable"
            advisory.append(fieldname)
        print(f"  {fieldname:18s} {k:7.3f}  {reading}")

    if advisory:
        print("\nFields with kappa < 0.60:", ", ".join(advisory))
        print("Two legitimate responses, per GOLDEN_SET_SPEC §4:")
        print("  1. merge the confusable values in taxonomy.yaml, or")
        print("  2. drop the field's CI floor and treat it as advisory.")
        print("Record which, and why, in eval/golden/IAA.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
