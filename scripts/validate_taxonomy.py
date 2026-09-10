import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
with (ROOT / "config" / "taxonomy.yaml").open() as fh:
    t = yaml.safe_load(fh)
errors, warnings = [], []

slot_ids = {s["id"] for s in t["slots"]}
subs = t["subcategories"]

# every subcategory group maps to a real slot
for group in subs:
    if group not in slot_ids:
        errors.append(f"subcategory group '{group}' is not a slot")

# no duplicate subcategories across slots
flat = [s for g in subs.values() for s in g]
dupes = [k for k, v in Counter(flat).items() if v > 1]
if dupes:
    errors.append(f"duplicate subcategories: {dupes}")

# outfit rules reference real slots
r = t["outfit_rules"]
for combo in r["exactly_one_of"]:
    for s in combo:
        if s not in slot_ids:
            errors.append(f"exactly_one_of unknown slot {s}")
for s in r["exactly_one"] + r["at_most_one"]:
    if s not in slot_ids:
        errors.append(f"unknown slot {s}")
for rng in r["ranges"]:
    if rng["slot"] not in slot_ids:
        errors.append(f"range unknown slot {rng['slot']}")
    if rng["min"] > rng["max"]:
        errors.append(f"range {rng['slot']}: min > max")

# requires: subcategory exists, target slot exists, hint is in that slot
for req in r["requires"]:
    if req["subcategory"] not in flat:
        errors.append(f"requires: unknown subcategory {req['subcategory']}")
    if req["needs_slot"] not in slot_ids:
        errors.append(f"requires: unknown slot {req['needs_slot']}")
    if req.get("hint") and req["hint"] not in subs.get(req["needs_slot"], []):
        errors.append(f"requires: hint {req['hint']} not in slot {req['needs_slot']}")

# forbidden pairs / slot_overrides reference real subcategories
for pair in r["forbidden_pairs"]:
    for s in pair:
        if s not in flat:
            errors.append(f"forbidden_pairs unknown subcategory {s}")
for ov in t["slot_overrides"]:
    if ov["subcategory"] not in flat:
        errors.append(f"slot_override unknown sub {ov['subcategory']}")
    if ov["also_slot"] not in slot_ids:
        errors.append(f"slot_override unknown slot {ov['also_slot']}")

# penalty rules: more_than_two_bold_patterns must not carry a hand-listed
# value set that can drift from patterns[].bold (this happened once already)
for p in r["penalties"]:
    if p["rule"] == "more_than_two_bold_patterns" and "note" in p:
        note_text = p["note"]
        if any(f"= {x}" in note_text or f"={x}" in note_text for x in ["graphic_print", "floral"]):
            errors.append(
                "penalty note hand-lists bold patterns again — must defer to patterns[].bold"
            )

# ATR mapping targets real slots
for k, v in t["atr_to_slot"].items():
    if v is not None and v not in slot_ids:
        errors.append(f"atr_to_slot: {k} -> unknown slot {v}")

# dress code compatibility is symmetric and closed
dc_ids = {d["id"] for d in t["dress_codes"]}
comp = t["dress_code_compatibility"]
if set(comp) != dc_ids:
    errors.append(f"dress_code_compatibility keys != dress_codes: {set(comp) ^ dc_ids}")
for a, lst in comp.items():
    for b in lst:
        if b not in dc_ids:
            errors.append(f"compat: unknown dress_code {b}")
        elif a not in comp.get(b, []):
            warnings.append(f"compat asymmetric: {a}->{b} but not {b}->{a}")

# formality ranges within 1..5
for d in t["dress_codes"]:
    lo, hi = d["formality_range"]
    if not (1 <= lo <= hi <= 5):
        errors.append(f"{d['id']} bad formality_range")

# occasions reference real dress codes; formality inside that code's range
for o in t["occasions"]:
    if o["dress_code"] not in dc_ids:
        errors.append(f"occasion {o['id']}: unknown dress_code")
    else:
        lo, hi = next(d["formality_range"] for d in t["dress_codes"] if d["id"] == o["dress_code"])
        if not lo <= o["formality_target"] <= hi:
            errors.append(
                f"occasion {o['id']}: formality {o['formality_target']} "
                f"outside {o['dress_code']} range {lo}-{hi}"
            )

# colours: unique ids, hex present except multi
seen = set()
for c in t["colours"]:
    if c["id"] in seen:
        errors.append(f"duplicate colour {c['id']}")
    seen.add(c["id"])
    if c["family"] != "multi" and not c.get("hex"):
        errors.append(f"colour {c['id']} missing hex")

# fields reference known tiers; eval_floors match field names
for f, cfg in t["fields"].items():
    if cfg["tier"] not in {"local", "vlm", "rule"}:
        errors.append(f"field {f} bad tier")
    if (
        cfg["tier"] == "rule"
        and cfg["source"] == "derived"
        and f == "climate_bands"
        and "climate_band_rules" not in t
    ):
        errors.append(f"field {f} is tier=rule/derived but no `climate_band_rules` table exists")
for f in t["eval_floors"]:
    if f != "by_slice" and f not in t["fields"]:
        errors.append(f"eval_floor for unknown field {f}")

# eval_floors.by_slice must cover every slice GOLDEN_SET_SPEC.md quotas
GOLDEN_SLICES = {
    "flat_lay",
    "on_hanger",
    "worn_single",
    "worn_multi",
    "ethnic_wear",
    "dark_on_dark",
    "pattern_heavy",
    "folded",
    "low_light",
}
by_slice = set(t["eval_floors"].get("by_slice", {}))
if GOLDEN_SLICES - by_slice:
    errors.append(f"eval_floors.by_slice missing slices: {sorted(GOLDEN_SLICES - by_slice)}")
if by_slice - GOLDEN_SLICES:
    warnings.append(
        "eval_floors.by_slice has slices not in GOLDEN_SET_SPEC.md: "
        f"{sorted(by_slice - GOLDEN_SLICES)}"
    )


# climate_bands must be a TOTAL partition of (temp, humidity, precip) space:
# every combination must resolve to at least one band under first-match
# evaluation order. Swept as a grid rather than proven symbolically — cheap,
# and it's exactly this class of bug (gaps, double-matches at boundaries)
# that broke this section before.
def band_matches(b, temp, hum, precip):
    lo, hi = b["temp_c"]  # half-open [lo, hi); None = unbounded
    if lo is not None and temp < lo:
        return False
    if hi is not None and temp >= hi:
        return False
    if b.get("precip") and not precip:
        return False
    if "humidity_pct" in b:
        h1, h2 = b["humidity_pct"]
        if not (h1 <= hum <= h2):
            return False
    return True


bands = t["climate_bands"]
gaps = []
for temp in range(-10, 51, 2):
    for hum in range(0, 101, 10):
        for precip in (False, True):
            if not any(band_matches(b, temp, hum, precip) for b in bands):
                gaps.append((temp, hum, precip))
if gaps:
    errors.append(
        f"climate_bands: {len(gaps)} (temp,humidity,precip) combinations "
        f"match NO band, e.g. {gaps[:3]}"
    )

# fields marked required with no producer rule
for f, c in t["fields"].items():
    if (
        c["required"]
        and c["tier"] == "rule"
        and c["source"] == "derived"
        and "climate_band_rules" not in t
    ):
        errors.append(f"field {f}: required + derived but no derivation table in taxonomy.yaml")

# no 'other' anywhere
if any(s == "other" for s in flat) or "other" in t["materials"]:
    errors.append("an 'other' value exists — forbidden by design")

print(f"slots                {len(slot_ids)}")
print(f"subcategories        {len(flat)}")
print(f"colours              {len(t['colours'])}")
print(f"materials            {len(t['materials'])}")
_bold = sum(1 for p in t["patterns"] if p["bold"])
print(f"patterns             {len(t['patterns'])}  ({_bold} bold)")
print(f"fits                 {len(t['fits'])}")
print(f"dress_codes          {len(dc_ids)}")
print(f"occasions            {len(t['occasions'])}")
_vlm = sum(1 for c in t["fields"].values() if c["tier"] == "vlm")
print(f"fields               {len(t['fields'])}  ({_vlm} need the VLM)")
print()
for w in warnings:
    print("WARN ", w)
print()
print("ERRORS:", len(errors))
for e in errors:
    print("  ✗", e)
sys.exit(1 if errors else 0)
