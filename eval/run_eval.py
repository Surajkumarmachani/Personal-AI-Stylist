"""Golden-set evaluation harness (Step 3.5) — THE POINT OF PHASE 3.

    python eval/run_eval.py --extractor-version v0.1
    python eval/run_eval.py --slice ethnic_wear          # one slice only
    python eval/run_eval.py --baseline                   # write baselines/

Produces per-field accuracy SLICED by golden-set category. The overall number
is nearly useless and actively misleading: a set that is 24% clean flat-lays
will report a healthy average while failing completely on sarees. The slice
table is the product decision, which is why it is what gets printed and what
the CI floors are checked against.

WHAT THIS CANNOT TELL YOU TODAY
-------------------------------
There is no golden set yet. `eval/golden/labels.jsonl` does not exist, so this
script currently reports MISSING and exits 2 — deliberately distinct from both
"passed" (0) and "a floor was breached" (1), so CI can tell "we have not
measured this" apart from "we measured it and it is bad".

That distinction matters because the Phase 3 exit criterion is not "the harness
runs". It is a NUMBER: segmentation accuracy on the 50 ethnic-wear images, and
a recorded decision between (a) accepting manual crops for drapes, (b) adding a
drape detector, or (c) fine-tuning SegFormer. None of those can be chosen from
an unmeasured guess, and the harness existing does not substitute for the 500
labelled images that GOLDEN_SET_SPEC.md specifies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO_ROOT / "packages"), str(REPO_ROOT / "services")]
os.environ.setdefault("MODELS_ROOT", str(REPO_ROOT / "models"))

GOLDEN_DIR = REPO_ROOT / "eval" / "golden"
LABELS_PATH = GOLDEN_DIR / "labels.jsonl"
IMAGES_DIR = GOLDEN_DIR / "images"
BASELINES_DIR = REPO_ROOT / "eval" / "baselines"

# Exit codes are part of this script's contract with CI.
EXIT_OK = 0
EXIT_FLOOR_BREACHED = 1
EXIT_NO_GOLDEN_SET = 2

# Fields this harness can score today. `subcategory`, `material`, `formality`,
# `dress_code`, `warmth` and `fit` are all tier=vlm and arrive in Phase 4;
# scoring them now would report 0% and look like a regression rather than an
# absence.
SCORABLE_FIELDS = ("slot", "primary_colour", "secondary_colour")


@dataclass
class FieldScore:
    correct: int = 0
    total: int = 0
    # Predicted -> actual counts, for the confusion matrix. A single accuracy
    # number says something is wrong; the confusion matrix says what to fix.
    confusions: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


@dataclass
class Results:
    by_field: dict[str, FieldScore] = field(default_factory=lambda: defaultdict(FieldScore))
    by_slice: dict[str, dict[str, FieldScore]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(FieldScore))
    )
    images: int = 0
    # Garments the pipeline found that no label accounts for, and vice versa.
    # Split accuracy is a different question from attribute accuracy and both
    # matter: correct attributes on the wrong mask is not a success.
    split_exact: int = 0
    split_over: int = 0
    split_under: int = 0
    errors: list[str] = field(default_factory=list)


def load_labels() -> list[dict[str, Any]]:
    with LABELS_PATH.open() as fh:
        return [json.loads(line) for line in fh if line.strip()]


def report_missing_golden_set() -> int:
    print("GOLDEN SET NOT PRESENT — nothing measured.\n")
    print(f"  expected labels at: {LABELS_PATH.relative_to(REPO_ROOT)}")
    print(f"  expected images at: {IMAGES_DIR.relative_to(REPO_ROOT)}/\n")
    print("This is not a harness failure. Phase 0.2 specifies 500 hand-labelled")
    print("images (GOLDEN_SET_SPEC.md) and they do not exist yet, so no accuracy")
    print("number can be produced.\n")
    print("Blocked on it, specifically:")
    print("  - the Phase 3 exit criterion: measured segmentation accuracy on the")
    print("    50 ethnic-wear images")
    print("  - the (a) manual crop / (b) drape detector / (c) fine-tune decision,")
    print("    which the plan says must NOT be made before the number exists")
    print("  - the CI accuracy floors in taxonomy.yaml eval_floors, which are")
    print("    currently unenforceable\n")
    print(f"Exiting {EXIT_NO_GOLDEN_SET} (not-measured), distinct from 1 (floor breached).")
    return EXIT_NO_GOLDEN_SET


async def score_image(ml: Any, label: dict[str, Any], results: Results, taxonomy: Any) -> None:
    """Run the real pipeline stages over one labelled image and compare."""
    from stylist_domain.split import MaskInfo, SplitOutcome, split_masks

    image_path = GOLDEN_DIR / label["file"]
    if not image_path.is_file():
        results.errors.append(f"{label['image_id']}: image missing at {image_path}")
        return

    data = image_path.read_bytes()
    slices = label.get("slices") or ["unsliced"]
    truth = label["garments"]

    seg = await ml.segment(image_bytes=data)
    decision = split_masks(
        [
            MaskInfo(
                atr_label=m.atr_label,
                slot_hint=m.slot_hint,
                area_pct=m.area_pct,
                bbox=m.bbox,
                index=i,
            )
            for i, m in enumerate(seg.masks)
        ],
        skin_pct=seg.skin_pct,
    )
    results.images += 1

    if decision.outcome is SplitOutcome.NEEDS_REVIEW:
        # Counted as under-split rather than skipped: routing a saree to manual
        # review is the honest outcome, and the ethnic_wear slice needs to show
        # HOW OFTEN that happens. That rate is the (a)/(b)/(c) decision.
        results.split_under += 1
        for name in slices:
            results.by_slice[name]["split"].total += 1
        results.by_field["split"].total += 1
        return

    predicted = decision.candidates
    if len(predicted) == len(truth):
        results.split_exact += 1
        results.by_field["split"].correct += 1
    elif len(predicted) > len(truth):
        results.split_over += 1
    else:
        results.split_under += 1
    results.by_field["split"].total += 1
    for name in slices:
        results.by_slice[name]["split"].total += 1
        if len(predicted) == len(truth):
            results.by_slice[name]["split"].correct += 1

    # Attribute scoring pairs predictions to truth by slot. Pairing by ORDER
    # would score a correct shirt against a labelled pair of shoes and report
    # nonsense.
    truth_by_slot = {g["slot"]: g for g in truth}
    for candidate in predicted:
        expected = truth_by_slot.get(candidate.slot_hint)
        score = results.by_field["slot"]
        score.total += 1
        for name in slices:
            results.by_slice[name]["slot"].total += 1
        if expected is not None:
            score.correct += 1
            for name in slices:
                results.by_slice[name]["slot"].correct += 1
        else:
            score.confusions[(str(candidate.slot_hint), "—")] += 1


def print_report(results: Results, taxonomy: Any) -> int:
    floors = taxonomy.eval_floors
    by_slice_floors = floors.get("by_slice", {})
    breached: list[str] = []

    print(f"\nimages scored: {results.images}")
    print(
        f"split: exact={results.split_exact} over={results.split_over} under={results.split_under}"
    )

    print("\nPER-FIELD")
    print(f"  {'field':18s} {'accuracy':>9s} {'n':>6s} {'floor':>7s}")
    for name, score in sorted(results.by_field.items()):
        floor = floors.get(name)
        flag = ""
        if floor is not None and score.total and score.accuracy < floor:
            flag = "  BELOW FLOOR"
            breached.append(f"{name}: {score.accuracy:.3f} < {floor}")
        print(
            f"  {name:18s} {score.accuracy:9.3f} {score.total:6d} "
            f"{floor if floor is not None else '—':>7} {flag}"
        )

    print("\nPER-SLICE  (the table that actually decides things)")
    for slice_name in sorted(results.by_slice):
        floor = by_slice_floors.get(slice_name)
        print(f"  {slice_name}   floor={floor if floor is not None else '—'}")
        for field_name, score in sorted(results.by_slice[slice_name].items()):
            flag = ""
            if floor is not None and score.total and score.accuracy < floor:
                flag = "  BELOW FLOOR"
                breached.append(f"{slice_name}/{field_name}: {score.accuracy:.3f} < {floor}")
            print(f"      {field_name:16s} {score.accuracy:6.3f}  n={score.total:<5d}{flag}")

    if results.errors:
        print(f"\n{len(results.errors)} error(s):")
        for err in results.errors[:10]:
            print(f"  {err}")

    if breached:
        print("\nFLOORS BREACHED:")
        for item in breached:
            print(f"  {item}")
        print("\nNever lower a floor to make a build pass — that is how quality")
        print("silently regresses. Fix the extractor or record a deliberate,")
        print("dated exception.")
        return EXIT_FLOOR_BREACHED

    print("\nall floors met")
    return EXIT_OK


async def main_async(args: argparse.Namespace) -> int:
    from stylist_clients.ml_client import MLClient
    from stylist_domain.taxonomy import load_taxonomy

    taxonomy = load_taxonomy()
    labels = load_labels()
    if args.slice:
        labels = [item for item in labels if args.slice in (item.get("slices") or [])]
        if not labels:
            print(f"no images in slice {args.slice!r}")
            return EXIT_NO_GOLDEN_SET

    ml = MLClient(args.ml_url)
    ready = await ml.readyz()
    if not ready.get("ready"):
        print(f"ml service is not ready at {args.ml_url}: {ready}", file=sys.stderr)
        return EXIT_FLOOR_BREACHED

    results = Results()
    for label in labels:
        await score_image(ml, label, results, taxonomy)

    code = print_report(results, taxonomy)

    if args.baseline:
        BASELINES_DIR.mkdir(parents=True, exist_ok=True)
        out = BASELINES_DIR / f"{args.extractor_version}.json"
        out.write_text(
            json.dumps(
                {
                    "extractor_version": args.extractor_version,
                    "images": results.images,
                    "split": {
                        "exact": results.split_exact,
                        "over": results.split_over,
                        "under": results.split_under,
                    },
                    "by_field": {
                        k: {"accuracy": v.accuracy, "n": v.total}
                        for k, v in results.by_field.items()
                    },
                    "by_slice": {
                        s: {k: {"accuracy": v.accuracy, "n": v.total} for k, v in f.items()}
                        for s, f in results.by_slice.items()
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        print(f"\nbaseline written to {out.relative_to(REPO_ROOT)}")
    return code


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extractor-version", default="v0.1")
    ap.add_argument("--ml-url", default=os.environ.get("ML_BASE_URL", "http://localhost:8081"))
    ap.add_argument("--slice", help="score only one golden-set slice")
    ap.add_argument("--baseline", action="store_true", help="write eval/baselines/<version>.json")
    args = ap.parse_args()

    if not LABELS_PATH.is_file():
        return report_missing_golden_set()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
