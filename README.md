# Personal AI Stylist

A wardrobe cataloguing and outfit recommendation system, designed for Indian and
Western mixed wardrobes. Photograph your clothes, get them catalogued
automatically, get outfit suggestions that account for weather, occasion and
what you actually wear.

**Status:** Phase 0 complete (taxonomy frozen). Phase 1 not started.

## Documents

| Document | What it is |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Production architecture spec v1.0 — 5 views (trust boundaries, deployment, components, latency budget, ingest state machine) + 9 non-functional specs (SLOs, capacity, cost, consistency, failure, DR, ML lifecycle, release, observability) |
| [docs/implementation-plan.md](docs/implementation-plan.md) | 14-week phased build plan, Phases 0–11, with exit criteria per phase |
| [GOLDEN_SET_SPEC.md](GOLDEN_SET_SPEC.md) | The 500-image evaluation set: composition, label schema, consent, inter-annotator agreement |
| [config/taxonomy.yaml](config/taxonomy.yaml) | **Single source of truth** for all garment enums. Changing this file is a migration. |

## Repository layout

```
config/taxonomy.yaml          frozen enums — generates DB types, VLM schemas,
                              slot rules, ATR mapping, golden-set validation
scripts/validate_taxonomy.py  structural + cross-reference checks on the above
docs/                         architecture spec and implementation plan
GOLDEN_SET_SPEC.md            eval set collection and labelling spec
```

## Taxonomy validation

`config/taxonomy.yaml` is the single source of truth for every garment enum in
the system — Postgres types, VLM extraction schemas, slot legality rules, the
ATR→slot segmentation mapping, and golden-set label validation all derive from
it. Never hand-duplicate a value out of it.

```bash
pip install pyyaml
python scripts/validate_taxonomy.py
```

Exits non-zero on any structural error. Checks include: every subcategory maps
to a real slot, no duplicates, outfit rules and `requires` edges reference real
values, dress-code compatibility is symmetric and closed, occasion formality
targets sit inside their dress code's range, colours have hex anchors,
`climate_bands` is a total partition of (temperature, humidity, precipitation)
space with no gaps, every `tier: rule` field has a derivation table, and
`eval_floors.by_slice` covers every slice `GOLDEN_SET_SPEC.md` quotas.

This runs as a required CI check — the taxonomy is load-bearing on the schema,
so a broken one must not reach `main`.

## Design decisions worth knowing up front

Four decisions are recorded in `config/taxonomy.yaml` itself, with reasoning:

1. **A saree is `drape` + a required `upper_base`**, not `full_body` — users own
   and re-pair blouses independently, cost-per-wear is tracked per physical
   garment, and segmentation produces separate masks anyway.
2. **Formality and dress code are two independent axes.** A mehendi outfit and a
   boardroom suit are both formality 4; on a single scale they become
   interchangeable, which is the specific failure that makes generic wardrobe
   apps feel wrong in India.
3. **Climate bands replace four-season enums.** Bengaluru has no autumn, and
   monsoon is a first-class wardrobe constraint (fabric, footwear, hemline) with
   no Western-season equivalent.
4. **There is no `other` value anywhere, by design.** If a real garment cannot be
   classified, that is a taxonomy bug — add the value and bump the version.
