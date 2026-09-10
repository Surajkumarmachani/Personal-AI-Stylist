"""Golden-set label validation (GOLDEN_SET_SPEC.md section 2).

    python eval/validate_labels.py

Every label value must exist in config/taxonomy.yaml. That rule is what makes
the eval set and the model answer the same question: if a labeller writes
`crimson` and the taxonomy only has `red` and `maroon`, the model can never
score correctly on that row no matter how right it is.

GOLDEN_SET_SPEC is explicit that an unlabelable garment is a TAXONOMY BUG, not
a labelling problem — file it, add the value, bump the version. So this script
rejects unknown values rather than coercing them, because a coerced label is a
wrong label that scores as ground truth forever.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO_ROOT / "packages")]

from stylist_domain.taxonomy import load_taxonomy  # noqa: E402

LABELS_PATH = REPO_ROOT / "eval" / "golden" / "labels.jsonl"
IMAGES_DIR = REPO_ROOT / "eval" / "golden" / "images"

# GOLDEN_SET_SPEC section 1: minimum count per slice, not a partition —
# slices overlap (a folded black saree in low light gets four).
SLICE_QUOTAS = {
    "flat_lay": 120,
    "on_hanger": 80,
    "worn_single": 80,
    "worn_multi": 60,
    "ethnic_wear": 50,
    "dark_on_dark": 40,
    "pattern_heavy": 30,
    "folded": 20,
    "low_light": 20,
}
TOTAL_TARGET = 500

VALID_CONSENT = {
    "owner_consented_eval_only",
    "owner_consented_incl_training",
    "licensed_stock",
    "first_party_catalogue",
}
VALID_CONFIDENCE = {"high", "medium", "low"}


def main() -> int:
    if not LABELS_PATH.is_file():
        print(f"no labels at {LABELS_PATH.relative_to(REPO_ROOT)}")
        print("\nPhase 0.2 is not done. GOLDEN_SET_SPEC.md specifies 500 labelled")
        print("images; collection is the long-lead item and it gates Phase 3's")
        print("accuracy verdict. Nothing to validate yet.")
        return 2

    tax = load_taxonomy()
    errors: list[str] = []
    warnings: list[str] = []
    slice_counts: Counter[str] = Counter()
    seen_ids: set[str] = set()

    valid = {
        "slot": set(tax.slots),
        "subcategory": set(tax.subcategories),
        "primary_colour": set(tax.colours),
        "secondary_colour": set(tax.colours),
        "pattern": set(tax.patterns),
        "material": set(tax.materials),
        "fit": set(tax.fits),
        "dress_code": set(tax.dress_codes),
    }

    with LABELS_PATH.open() as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                label = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"line {lineno}: invalid JSON ({exc})")
                continue

            image_id = label.get("image_id", f"line{lineno}")
            if image_id in seen_ids:
                errors.append(f"{image_id}: duplicate image_id")
            seen_ids.add(image_id)

            if not (IMAGES_DIR.parent / label.get("file", "")).is_file():
                warnings.append(f"{image_id}: file not found ({label.get('file')})")

            if label.get("consent") not in VALID_CONSENT:
                errors.append(f"{image_id}: consent {label.get('consent')!r} not recognised")

            for name in label.get("slices") or []:
                slice_counts[name] += 1
                if name not in SLICE_QUOTAS:
                    errors.append(f"{image_id}: unknown slice {name!r}")

            garments = label.get("garments")
            if not isinstance(garments, list) or not garments:
                errors.append(f"{image_id}: `garments` must be a non-empty array")
                continue

            for garment in garments:
                gid = f"{image_id}#{garment.get('garment_index')}"
                for fieldname, allowed in valid.items():
                    value = garment.get(fieldname)
                    if value is None:
                        continue
                    if value not in allowed:
                        errors.append(
                            f"{gid}: {fieldname}={value!r} is not in taxonomy.yaml "
                            f"(v{tax.version}) — this is a taxonomy bug, not a "
                            f"labelling one: add the value and bump the version"
                        )
                for fieldname, lo, hi in (("formality", 1, 5), ("warmth", 1, 5)):
                    value = garment.get(fieldname)
                    if value is not None and not (lo <= value <= hi):
                        errors.append(f"{gid}: {fieldname}={value} outside {lo}-{hi}")
                conf = garment.get("labeller_confidence")
                if conf is not None and conf not in VALID_CONFIDENCE:
                    errors.append(f"{gid}: labeller_confidence={conf!r}")
                bbox = garment.get("bbox")
                if bbox is not None and (
                    len(bbox) != 4 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]
                ):
                    errors.append(f"{gid}: bbox {bbox} is not [x1,y1,x2,y2] with x1<x2, y1<y2")

    print(f"labels: {len(seen_ids)} images (target {TOTAL_TARGET})")
    print("\nslice quotas:")
    for name, quota in SLICE_QUOTAS.items():
        have = slice_counts.get(name, 0)
        mark = "ok " if have >= quota else "SHORT"
        print(f"  {mark} {name:16s} {have:4d} / {quota}")
        if have < quota:
            warnings.append(f"slice {name}: {have}/{quota}")

    for w in warnings[:20]:
        print(f"WARN  {w}")
    print(f"\nERRORS: {len(errors)}")
    for e in errors[:30]:
        print(f"  x {e}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
