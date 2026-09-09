# Golden Set — Collection and Labelling Spec

**Phase 0.2** · Runs in parallel with everything until Phase 3 · Owner: _________

The golden set is the only objective answer to "is our tagging good enough."
It gates CI from Phase 3 onward. Every accuracy claim in this project traces
back to these 500 images, so the composition matters more than the count.

**Start today.** Collection is the slowest thing in Phase 0 and it blocks the
Phase 3 go/no-go decision.

---

## 1. Composition — 500 images

Weighted toward failure on purpose. A set of 500 clean flat-lays would report
96% accuracy and tell you nothing about production.

| Slice | Count | What it tests | Why it's here |
|---|---|---|---|
| `flat_lay` | 120 | Baseline. Single garment, plain background. | Your ceiling. If this isn't ≥95%, something is broken upstream. |
| `on_hanger` | 80 | Shape distortion, background clutter | How most people actually photograph clothes |
| `worn_single` | 80 | One garment visible on a person | Skin/hair adjacency, pose |
| `worn_multi` | 60 | Mirror selfie, 3+ garments | **The split test.** Determines whether one-photo onboarding is viable |
| `ethnic_wear` | 50 | Saree drape, lehenga, kurta set, sherwani, dupatta | **The viability test.** ATR has never seen a saree |
| `dark_on_dark` | 40 | Black garment, dark background | Segmentation's worst case |
| `pattern_heavy` | 30 | Pattern on pattern | Colour extraction's worst case |
| `folded` | 20 | Stacked or folded in a drawer | Shape priors fail entirely |
| `low_light` | 20 | Backlit, tungsten, night indoor | Colour constancy |
| **Total** | **500** | | |

Slices are **not** mutually exclusive — a folded black saree in low light gets
all four tags. Record slices as a list, and the quota is a minimum per slice,
not a partition.

### The 50 ethnic-wear images, specified

This slice decides a Phase 3 architecture choice, so its internal
composition is prescribed rather than left to whoever is collecting:

| Garment | Count | Must include |
|---|---|---|
| Saree, draped on a person | 12 | 3+ drape styles (Nivi, Bengali, Gujarati); at least 2 with pallu over the head |
| Saree, folded / flat | 5 | Because most users will photograph it this way |
| Lehenga set (skirt + choli + dupatta) | 8 | At least 3 worn, showing all three pieces |
| Kurta / kurti | 8 | Range of lengths — the short/long boundary is where ATR flips between `Upper-clothes` and `Dress` |
| Salwar kameez set | 5 | With dupatta visible |
| Sherwani / bandhgala | 5 | Worn, full length |
| Dupatta / stole alone | 4 | Isolated, so you can see whether `Scarf` fires at all |
| Dhoti / angavastram | 3 | |

---

## 2. Label schema

`eval/golden/labels.jsonl` — one JSON object per line.

```json
{
  "image_id": "gs_0142",
  "file": "images/gs_0142.jpg",
  "slices": ["ethnic_wear", "worn_multi"],
  "source": "own_wardrobe",
  "consent": "owner_consented_eval_only",
  "garments": [
    {
      "garment_index": 0,
      "slot": "drape",
      "subcategory": "saree",
      "primary_colour": "maroon",
      "secondary_colour": "gold",
      "pattern": "zari_work",
      "material": "silk",
      "formality": 5,
      "dress_code": "formal_ethnic",
      "warmth": 2,
      "fit": "draped",
      "bbox": [120, 340, 780, 1600],
      "requires_companion": 1,
      "labeller_confidence": "high",
      "notes": "pallu over left shoulder, Nivi drape"
    },
    {
      "garment_index": 1,
      "slot": "upper_base",
      "subcategory": "choli_blouse",
      "primary_colour": "maroon",
      "secondary_colour": null,
      "pattern": "solid",
      "material": "silk",
      "formality": 5,
      "dress_code": "formal_ethnic",
      "warmth": 1,
      "fit": "tailored",
      "bbox": [280, 340, 620, 640],
      "labeller_confidence": "medium",
      "notes": "partially occluded by pallu"
    }
  ]
}
```

### Rules

- **Every value must exist in `config/taxonomy.yaml`.** The validator rejects
  anything else. If you cannot label a real garment, that is a taxonomy bug —
  file it, add the value, bump the taxonomy version. Do not invent a label.
- `garments` is an array even for single-garment images. `worn_multi` images
  have one entry per visible garment — that array length **is** the split
  ground truth.
- `bbox` is `[x1, y1, x2, y2]` in original-image pixels. Needed to score
  segmentation IoU separately from attribute accuracy — otherwise a correct
  attribute on the wrong mask counts as a pass.
- `requires_companion` points at the `garment_index` of the linked piece
  (saree → its blouse). Tests the REQUIRES rule end to end.
- `secondary_colour` is `null` unless a second hue covers >15% of the garment.
  Be strict; inconsistency here is the most common source of low agreement.
- `labeller_confidence` is `high | medium | low`. **Low-confidence labels are
  excluded from the CI floor** but retained for analysis. Do not delete them —
  a cluster of low-confidence labels on one field is itself a finding.
- `notes` is free text and is never scored. Use it generously; future-you
  needs it when a slice regresses.

---

## 3. Sourcing and consent

**Priority order:**

1. **Your own and your team's wardrobes.** Free, consented, realistic. Start here.
2. **Machani Group catalogue imagery**, if section 3.7 is retail-attached.
   Studio-clean, so tag these `flat_lay` and don't let them inflate the set —
   cap at 80 images or your baseline becomes fictional.
3. **Friends and family**, with written consent.
4. **Licensed stock**, for gaps only. Check the licence permits ML evaluation.

**Do not scrape.** Not Pinterest, not Instagram, not retailer sites. An eval
set is a permanent asset; you cannot later prove provenance for something you
scraped, and this set will be referenced in every accuracy claim you make.

**Consent record.** Every image carries a `consent` value:

| Value | Meaning |
|---|---|
| `owner_consented_eval_only` | Contributor agreed to internal evaluation use |
| `owner_consented_incl_training` | Also permits fine-tuning |
| `licensed_stock` | Licence file referenced in `eval/golden/LICENCES.md` |
| `first_party_catalogue` | Company-owned imagery |

Only `owner_consented_incl_training` images may be used if you fine-tune
SegFormer in Phase 3. Track this now; retrofitting consent is impossible.

**Faces.** Crop or blur faces in `worn_*` images unless the person explicitly
consented to their face being retained. Face pixels add nothing to garment
evaluation and turn a benign eval set into a biometric dataset you have to
govern. Blur before committing, not after.

---

## 4. Inter-annotator agreement

Two labellers. A **50-image overlap set**, labelled independently before
either touches the remaining 450.

Compute per-field Cohen's κ:

| κ | Reading | Action |
|---|---|---|
| > 0.80 | Strong | Proceed |
| 0.60–0.80 | Moderate | Tighten the labelling guide for that field, re-do the overlap |
| < 0.60 | Poor | **The field is unmeasurable.** Either the taxonomy values overlap, or the field is genuinely subjective |

**This is the most important step in Phase 0.2, and the one teams skip.** If
two humans agree only 55% of the time on `formality`, you cannot hold a model
to 75% — and any eval floor you set on that field is noise. Two legitimate
responses: merge the confusable values in the taxonomy, or drop the field's CI
floor and treat it as advisory.

Fields I expect to score low, based on how they're defined: `material`
(silk vs satin vs crepe from a photo is genuinely hard), `formality` on
smart-casual items, `fit` on draped garments. Fields that should score high:
`slot`, `primary_colour`, `pattern`.

Record results in `eval/golden/IAA.md` with the date, the two labellers, and
per-field κ. Re-run when the taxonomy version bumps.

---

## 5. Directory layout

```
eval/
├── GOLDEN_SET_SPEC.md          # this file
├── golden/
│   ├── images/                 # gs_0001.jpg … gs_0500.jpg
│   ├── labels.jsonl
│   ├── overlap/                # the 50-image IAA subset
│   │   ├── labeller_a.jsonl
│   │   └── labeller_b.jsonl
│   ├── IAA.md
│   ├── LICENCES.md
│   └── MANIFEST.json           # sha256 per image, taxonomy_version, counts
├── validate_labels.py          # schema + taxonomy conformance + quota check
├── compute_iaa.py              # per-field Cohen's κ
└── run_eval.py                 # Phase 3 — the CI gate
```

Images go to object storage, referenced by `MANIFEST.json` with checksums.
Only `labels.jsonl` and the manifest are committed to git — a 500-image
binary set in version control makes every clone painful.

---

## 6. Progress tracker

Update as you go. Collection is done when every slice hits quota **and**
`validate_labels.py` exits 0.

| Slice | Target | Collected | Labelled | Validated |
|---|---:|---:|---:|:---:|
| flat_lay | 120 | | | ☐ |
| on_hanger | 80 | | | ☐ |
| worn_single | 80 | | | ☐ |
| worn_multi | 60 | | | ☐ |
| **ethnic_wear** | **50** | | | ☐ |
| dark_on_dark | 40 | | | ☐ |
| pattern_heavy | 30 | | | ☐ |
| folded | 20 | | | ☐ |
| low_light | 20 | | | ☐ |

- [ ] 50-image overlap labelled independently by both labellers
- [ ] `compute_iaa.py` run; per-field κ recorded in `IAA.md`
- [ ] Fields with κ < 0.60 either merged in the taxonomy or demoted to advisory
- [ ] Faces blurred where consent doesn't cover retention
- [ ] `MANIFEST.json` generated with checksums + taxonomy version
- [ ] `validate_labels.py` exits 0

---

## 7. Exit criterion

> 500 labelled images, all slice quotas met, every value conformant to
> `taxonomy.yaml` v1.0.0, per-field κ recorded, and `validate_labels.py`
> green in CI.

At that point `run_eval.py` can be written (Phase 3), and Phase 3 can render
the ethnic-wear verdict that decides whether you accept manual cropping for
drapes, add a drape detector, or fine-tune SegFormer.

**Do not build the segmentation pipeline before this set exists.** You would
have no way to know whether it works.
