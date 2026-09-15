"""The validator: six assertions between a stochastic model and a user (§C3).

THIS IS A HARD GATE WITH A DETERMINISTIC FALLBACK, NOT A LINT STEP
------------------------------------------------------------------
It is the reason an LLM can sit on a user-facing path at all. Every assertion
that fails produces the SAME outcome — the deterministic ranking the scorer
already computed — so a rejected rerank costs the user nothing but the model's
opinion. Nothing here can fail open: `validate()` returns a verdict, never
raises, and the caller cannot accidentally use an unvalidated response because
the accepted ordering is only available inside `Accepted`.

WHY PURE
--------
No DB, no network, no clock. The caller passes in the two ID sets (what was
sent, what is still live) and the rest is a function of its arguments. That is
what makes the phase's defining test — a fabricated garment ID, then a valid ID
belonging to a DIFFERENT TENANT — a millisecond unit test rather than a
fixture-heavy integration one.

ORDER MATTERS AND IS PART OF THE CONTRACT
-----------------------------------------
Assertions run in §C3's order and STOP at the first failure. A response with a
fabricated ID and a 60-word rationale is reported as `unknown_id`, not
`rationale`: the metric is a regression signal, and a signal that reports
whichever rule happened to be checked last tells you nothing about what
changed. The one exception is assertion 6, which DEMOTES rather than rejects —
low confidence means "the model is unsure", not "the model is lying".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from stylist_domain.slots import OutfitItem, evaluate

# Assertion 5's limits.
#
# 40 WORDS is from §C3. A rationale is a caption, not an essay — past roughly a
# sentence or two nobody reads it, and a long one is usually the model padding
# because it has nothing specific to say.
MAX_RATIONALE_WORDS = 40

# Anything that looks like a link or a price. Both are the same class of
# failure: the model inventing commerce we did not ask for and cannot stand
# behind. A hallucinated price on someone's own trousers is absurd; a
# hallucinated URL is a phishing vector with our name on it.
_URL_RE = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|in|net|org|io|co)\b)", re.I)
_PRICE_RE = re.compile(r"(₹|rs\.?\s*\d|inr\s*\d|\$\s*\d|\d+\s*(rupees|dollars)|\bprice\b)", re.I)

# Body-shaming terms. DELIBERATELY CONSERVATIVE, and it includes words the
# fashion industry uses without a second thought.
#
# "Slimming", "flattering" and "hides" are the ordinary vocabulary of clothing
# copy, and every one of them presupposes that the wearer's body is a problem
# the garment is solving. This product suggests clothes from a wardrobe someone
# already owns; it has no business grading the body underneath. Excluding a
# merely bland rationale costs one template fallback. Shipping one that tells
# someone their stomach needs hiding is the kind of thing people screenshot.
#
# This is the regex half of §C3's "regex + small classifier". The classifier is
# NOT built, and that is a real gap rather than a decision: a term list cannot
# catch a sentence that is cruel without using any listed word. Listed here so
# the limitation is visible instead of assumed away.
_BODY_TERMS = frozenset(
    {
        "slimming",
        "flattering",
        "flatters",
        "hide",
        "hides",
        "conceal",
        "conceals",
        "camouflage",
        "problem area",
        "problem areas",
        "muffin top",
        "love handles",
        "thunder thighs",
        "tummy",
        "belly",
        "figure flaw",
        "figure flaws",
        "pear shaped",
        "apple shaped",
        "curvy",
        "skinny",
        "fat",
        "chubby",
        "overweight",
        "slim down",
        "look thinner",
        "elongate your",
        "balance your",
    }
)


@dataclass(frozen=True)
class Rejected:
    """The model's output is not usable. The caller serves deterministic order.

    `rule` is the metric label (`validator.reject{rule=...}`). `detail` is for
    humans reading a log and is never shown to a user.
    """

    rule: str
    detail: str

    accepted: bool = field(default=False, init=False)


@dataclass(frozen=True)
class Accepted:
    """A reranked ordering that survived every assertion.

    `order` is the reranked garment-id tuples, most-recommended first.
    `rationales` maps an outfit's id-tuple to its rationale text.
    `demoted` lists outfits assertion 6 pushed below deterministic order —
    accepted, but not trusted ahead of the scorer.
    """

    order: list[tuple[str, ...]]
    rationales: dict[tuple[str, ...], str]
    demoted: list[tuple[str, ...]] = field(default_factory=list)

    accepted: bool = field(default=True, init=False)


Verdict = Accepted | Rejected


def validate(
    response: Any,
    *,
    input_outfits: list[tuple[str, ...]],
    active_ids: set[str],
    slots_by_id: dict[str, tuple[str, str]],
    min_confidence: float = 0.5,
) -> Verdict:
    """Run §C3's six assertions in order.

    `input_outfits`  every outfit we SENT, as a tuple of garment ids.
    `active_ids`     ids still `is_active` and owned by THIS tenant. The caller
                     reads this from the database at request time; a stale
                     precompute referencing a deleted garment dies here.
    `slots_by_id`    garment id -> (slot, subcategory), for the slot re-check.
    """
    # ---- 1. schema -----------------------------------------------------
    #
    # Structure only. The repair retry §C3 allows lives in the CALLER, because
    # retrying means another provider call and this module makes none. A second
    # failure is the caller's cue to stop, not ours.
    if not isinstance(response, dict):
        return Rejected("schema", f"response is {type(response).__name__}, not an object")
    raw_outfits = response.get("outfits")
    if not isinstance(raw_outfits, list) or not raw_outfits:
        return Rejected("schema", "`outfits` missing, not a list, or empty")

    parsed: list[tuple[tuple[str, ...], str, float]] = []
    for entry in raw_outfits:
        if not isinstance(entry, dict):
            return Rejected("schema", "an outfit entry is not an object")
        ids = entry.get("garment_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            return Rejected("schema", "`garment_ids` must be a non-empty list of strings")
        rationale = entry.get("rationale")
        if not isinstance(rationale, str):
            return Rejected("schema", "`rationale` must be a string")
        confidence = entry.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            return Rejected("schema", "`confidence` must be a number")
        if not 0.0 <= float(confidence) <= 1.0:
            return Rejected("schema", f"confidence {confidence} outside 0-1")
        parsed.append((tuple(ids), rationale, float(confidence)))

    # ---- 2. output ids are a subset of input ids -----------------------
    #
    # THE ASSERTION THAT MAKES INVENTED CLOTHING STRUCTURALLY IMPOSSIBLE.
    # An exact set check against the ids we sent, not a name or a fuzzy match:
    # the model cannot dress the user in a jacket they do not own, no matter
    # what the prompt says, because an id it invented is not in this set.
    #
    # A garment belonging to ANOTHER TENANT fails here too, and for the same
    # reason — it was never in the input — which is why one assertion covers
    # both halves of this phase's defining test.
    sent_ids = {gid for outfit in input_outfits for gid in outfit}
    for ids, _, _ in parsed:
        unknown = [gid for gid in ids if gid not in sent_ids]
        if unknown:
            return Rejected("unknown_id", f"ids not in the input set: {sorted(unknown)[:5]}")

    # An outfit must also be one we SENT, not a fresh combination assembled
    # from sent garments. Reranking reorders; it does not design. A novel
    # combination has never been through the slot rules or the scorer.
    sent_outfits = {tuple(sorted(o)) for o in input_outfits}
    for ids, _, _ in parsed:
        if tuple(sorted(ids)) not in sent_outfits:
            return Rejected("unknown_outfit", f"outfit not among those sent: {sorted(ids)}")

    # ---- 3. still active and tenant-owned ------------------------------
    #
    # Guards the stale precompute: last night's suggestion can reference a
    # garment deleted this morning. Separate from assertion 2 because the
    # failures mean different things — `unknown_id` is a model that invented
    # something, `inactive` is our own cache being out of date, and conflating
    # them would make the reject metric unreadable as a quality signal.
    for ids, _, _ in parsed:
        gone = [gid for gid in ids if gid not in active_ids]
        if gone:
            return Rejected("inactive", f"garments no longer active/owned: {sorted(gone)[:5]}")

    # ---- 4. slot legality ----------------------------------------------
    #
    # Re-checked through the SAME evaluator the candidate generator uses. A
    # second implementation here could disagree with the first, and then the
    # validator would reject outfits the deterministic path itself produced —
    # a fallback that rejects its own fallback.
    for ids, _, _ in parsed:
        missing = [gid for gid in ids if gid not in slots_by_id]
        if missing:
            return Rejected("slot_illegal", f"no slot known for {sorted(missing)[:5]}")
        items = [OutfitItem(gid, *slots_by_id[gid]) for gid in ids]
        result = evaluate(items)
        if not result.valid:
            return Rejected("slot_illegal", f"{sorted(ids)}: {', '.join(result.violations)}")

    # ---- 5. the rationale ----------------------------------------------
    for ids, rationale, _ in parsed:
        problem = _rationale_problem(rationale)
        if problem is not None:
            return Rejected("rationale", f"{sorted(ids)}: {problem}")

    # ---- 6. confidence: DEMOTE, do not reject --------------------------
    #
    # An unsure model is not a lying model. Rejecting the whole response
    # because one outfit scored 0.4 throws away five good rerank decisions to
    # punish a model for being honest about the sixth. Demoted outfits keep
    # their place in the list but fall BELOW everything the scorer ranked
    # deterministically, which is the ordering the caller applies.
    confident = [(ids, r) for ids, r, c in parsed if c >= min_confidence]
    demoted = [ids for ids, _, c in parsed if c < min_confidence]

    return Accepted(
        order=[ids for ids, _ in confident],
        rationales=dict(confident),
        demoted=demoted,
    )


def _rationale_problem(text: str) -> str | None:
    """Assertion 5's checks. Returns a reason, or None when the text is fine."""
    words = text.split()
    if len(words) > MAX_RATIONALE_WORDS:
        return f"rationale is {len(words)} words (max {MAX_RATIONALE_WORDS})"
    if _URL_RE.search(text):
        return "rationale contains a URL"
    if _PRICE_RE.search(text):
        return "rationale mentions a price"

    # Matched on a punctuation-stripped, lowercased form so that "Slimming!"
    # and "slimming" are the same word. Multi-word terms are checked against
    # the whole normalised string; single words against the token set, so
    # "fat" does not fire on "comfortable".
    normalised = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    tokens = set(normalised.split())
    for term in _BODY_TERMS:
        if " " in term:
            if term in normalised:
                return f"rationale contains body-shaming language: {term!r}"
        elif term in tokens:
            return f"rationale contains body-shaming language: {term!r}"
    return None
