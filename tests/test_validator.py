"""Phase 7.2 — the six assertions of §C3.

THE TEST THAT DEFINES THE PHASE is `test_a_fabricated_garment_id_is_rejected`
and its cross-tenant twin immediately below it. Everything else in this file
exists to stop those two passing for the wrong reason.

No fixtures, no database, no gateway: `validate()` is pure, so the caller
supplies the two id sets and the whole contract is exercisable in milliseconds.
That is the argument for the module being pure in the first place.
"""

from __future__ import annotations

import uuid

from stylist_domain.validator import MAX_RATIONALE_WORDS, Accepted, Rejected, validate

# A legal outfit: one upper_base, one lower, one feet.
TOP = str(uuid.uuid4())
BOTTOM = str(uuid.uuid4())
SHOES = str(uuid.uuid4())
OUTFIT = (TOP, BOTTOM, SHOES)

SLOTS = {
    TOP: ("upper_base", "shirt"),
    BOTTOM: ("lower", "trousers"),
    SHOES: ("feet", "sneakers"),
}


def response(ids=OUTFIT, *, rationale="Clean and comfortable for a warm day.", confidence=0.9):
    return {
        "outfits": [{"garment_ids": list(ids), "rationale": rationale, "confidence": confidence}]
    }


def run(resp, *, input_outfits=None, active=None, slots=None, **kw):
    return validate(
        resp,
        input_outfits=input_outfits if input_outfits is not None else [OUTFIT],
        active_ids=active if active is not None else set(OUTFIT),
        slots_by_id=slots if slots is not None else SLOTS,
        **kw,
    )


# ------------------------------------------------- the defining tests


def test_a_fabricated_garment_id_is_rejected() -> None:
    """§C3 assertion 2, and the reason an LLM can touch this path at all.

    A model that invents a garment must not be able to dress the user in it.
    The check is exact set membership against the ids we SENT — not a name
    match, not a similarity threshold — so this is structural rather than
    prompt-dependent. No amount of prompt drift makes an invented id a member
    of a set it was never in.
    """
    invented = str(uuid.uuid4())
    verdict = run(response([TOP, BOTTOM, invented]))

    assert isinstance(verdict, Rejected)
    assert verdict.rule == "unknown_id"
    assert invented in verdict.detail
    # The caller serves deterministic order; there is no partial acceptance to
    # be tempted by, because a Rejected carries no ordering at all.
    assert not hasattr(verdict, "order")


def test_a_valid_id_from_another_tenant_is_rejected_identically() -> None:
    """The second half of the defining test.

    A real, active, perfectly well-formed garment id — belonging to someone
    else. It fails at the SAME assertion as an invented id, because the input
    set is per-request and another tenant's garment was never in it. One
    assertion covering both is the point: there is no separate cross-tenant
    code path that could be forgotten, and RLS is not being asked to do a job
    the validator should do.
    """
    other_tenants_shirt = str(uuid.uuid4())
    verdict = run(
        response([other_tenants_shirt, BOTTOM, SHOES]),
        # Note it IS active — in the other tenant's wardrobe. Being live is not
        # the question; being ours is.
        active=set(OUTFIT) | {other_tenants_shirt},
        slots={**SLOTS, other_tenants_shirt: ("upper_base", "shirt")},
    )

    assert isinstance(verdict, Rejected)
    assert verdict.rule == "unknown_id"


# ------------------------------------------------- assertion 1: schema


def test_structurally_broken_responses_are_rejected_not_raised() -> None:
    """The validator never raises. A gate that can throw is a gate that can
    take down the endpoint it was added to protect."""
    for broken in (
        None,
        [],
        "a string",
        {},
        {"outfits": []},
        {"outfits": "not a list"},
        {"outfits": [{"garment_ids": [], "rationale": "x", "confidence": 0.9}]},
        {"outfits": [{"garment_ids": [TOP], "rationale": 5, "confidence": 0.9}]},
        {"outfits": [{"garment_ids": [TOP], "rationale": "x", "confidence": "high"}]},
        {"outfits": [{"garment_ids": [TOP], "rationale": "x", "confidence": 1.7}]},
    ):
        verdict = run(broken)
        assert isinstance(verdict, Rejected), broken
        assert verdict.rule == "schema", broken


def test_a_boolean_confidence_is_not_a_number() -> None:
    """`isinstance(True, int)` is True in Python, so a naive numeric check
    accepts `confidence: true` and then compares it to a threshold as 1.0 —
    a fabricated certainty that reads as the model's highest possible score."""
    verdict = run(response(confidence=True))
    assert isinstance(verdict, Rejected) and verdict.rule == "schema"


# ------------------------------------- assertion 2b: no novel combinations


def test_the_model_may_reorder_but_not_design() -> None:
    """Every sent garment, recombined into an outfit we never sent.

    Assertion 2 alone would pass this — every id is in the input set. But the
    combination has never been through the slot rules or the scorer, so
    accepting it would let the reranker introduce outfits the deterministic
    pipeline rejected. Reranking reorders; it does not design.
    """
    other_top, sandals = str(uuid.uuid4()), str(uuid.uuid4())
    sent_b = (other_top, BOTTOM, sandals)
    # Every id below WAS sent — TOP in outfit A, `sandals` in outfit B — but
    # this particular trio never was. It is even slot-legal, which is the
    # point: legality is not the same as having been generated and scored.
    novel = (TOP, BOTTOM, sandals)

    verdict = validate(
        response(novel),
        input_outfits=[OUTFIT, sent_b],
        active_ids=set(OUTFIT) | set(sent_b),
        slots_by_id={
            **SLOTS,
            other_top: ("upper_base", "kurta"),
            sandals: ("feet", "sandals"),
        },
    )
    assert isinstance(verdict, Rejected)
    assert verdict.rule == "unknown_outfit"


# ------------------------------------------- assertion 3: still ours, still live


def test_a_garment_deleted_since_the_precompute_is_rejected() -> None:
    """The stale-precompute guard, and it reports a DIFFERENT rule.

    `unknown_id` means a model invented something; `inactive` means our own
    overnight cache is out of date. Same user-visible outcome, but conflating
    them in the metric would make `validator.reject{rule=...}` useless as the
    quality signal §C3 says it is — a spike in one is a model regression, a
    spike in the other is a cache TTL problem.
    """
    verdict = run(response(), active={TOP, BOTTOM})  # SHOES deleted this morning
    assert isinstance(verdict, Rejected)
    assert verdict.rule == "inactive"
    assert SHOES in verdict.detail


# ------------------------------------------------- assertion 4: slot legality


def test_slot_illegal_outfits_are_rejected() -> None:
    """Two footwear. Checked through the same evaluator the generator uses, so
    the validator cannot reject an outfit the deterministic path produced."""
    second_shoe = str(uuid.uuid4())
    ids = (TOP, BOTTOM, SHOES, second_shoe)
    verdict = validate(
        response(ids),
        input_outfits=[ids],
        active_ids=set(ids),
        slots_by_id={**SLOTS, second_shoe: ("feet", "loafers")},
    )
    assert isinstance(verdict, Rejected)
    assert verdict.rule == "slot_illegal"


# ------------------------------------------------- assertion 5: the rationale


def test_an_overlong_rationale_is_rejected() -> None:
    verdict = run(response(rationale="word " * (MAX_RATIONALE_WORDS + 1)))
    assert isinstance(verdict, Rejected) and verdict.rule == "rationale"


def test_urls_and_prices_are_rejected() -> None:
    """Both are the model inventing commerce. A hallucinated price on clothes
    the user already owns is absurd; a hallucinated URL is a phishing vector
    carrying our name."""
    for bad in (
        "Great look — see https://example.com for more.",
        "Pairs well, and it only cost ₹2,400.",
        "A steal at $40.",
        "Check www.shop.in for similar.",
    ):
        verdict = run(response(rationale=bad))
        assert isinstance(verdict, Rejected), bad
        assert verdict.rule == "rationale", bad


def test_body_shaming_language_is_rejected_including_the_polite_kind() -> None:
    """The listed terms are ordinary fashion copy, and that is exactly why.

    "Slimming" and "flattering" presuppose the wearer's body is a problem the
    garment solves. This product suggests clothes someone already owns and has
    no business grading the body underneath. A blocked bland rationale costs
    one template fallback; a shipped one gets screenshotted.
    """
    for bad in (
        "This kurta is very slimming.",
        "A flattering cut for your shape.",
        "Hides your tummy nicely.",
        "Great for camouflaging problem areas.",
    ):
        verdict = run(response(rationale=bad))
        assert isinstance(verdict, Rejected), bad
        assert verdict.rule == "rationale", bad


def test_an_ordinary_rationale_passes() -> None:
    """The counterweight: a term list tuned until nothing passes is not a
    safety feature, it is a permanently-broken reranker."""
    for good in (
        "Cotton keeps this breathable for a warm, humid afternoon.",
        "The muted palette suits an interview.",
        "Light layers for an evening that cools down.",
    ):
        verdict = run(response(rationale=good))
        assert isinstance(verdict, Accepted), good


def test_substrings_do_not_trigger_the_body_term_list() -> None:
    """`fat` must not fire on `comfortable`. A false positive here silently
    disables the reranker for ordinary text, which looks like the model being
    bad rather than the filter being wrong."""
    verdict = run(response(rationale="Comfortable and breathable in the heat."))
    assert isinstance(verdict, Accepted)


# ------------------------------------------ assertion 6: demote, never reject


def test_low_confidence_demotes_rather_than_rejecting() -> None:
    """An unsure model is not a lying model.

    Rejecting the whole response because one outfit scored 0.4 discards the
    model's good decisions to punish it for being honest about a weak one.
    """
    second = (BOTTOM, TOP, SHOES)
    resp = {
        "outfits": [
            {
                "garment_ids": list(OUTFIT),
                "rationale": "Breathable for the heat.",
                "confidence": 0.9,
            },
            {"garment_ids": list(second), "rationale": "Might work.", "confidence": 0.2},
        ]
    }
    verdict = validate(
        resp,
        input_outfits=[OUTFIT, second],
        active_ids=set(OUTFIT),
        slots_by_id=SLOTS,
        min_confidence=0.5,
    )
    assert isinstance(verdict, Accepted)
    assert verdict.order == [OUTFIT]
    assert verdict.demoted == [second]
    assert second not in verdict.rationales


# ------------------------------------------------- ordering is the contract


def test_assertions_stop_at_the_first_failure_in_spec_order() -> None:
    """A response that breaks assertions 2 AND 5 reports `unknown_id`.

    The reject metric is a regression signal. One that reports whichever rule
    happened to be evaluated last tells you nothing about what changed, so the
    order in §C3 is part of the contract rather than an implementation detail.
    """
    verdict = run(
        response(
            [TOP, BOTTOM, str(uuid.uuid4())],
            rationale="This is very slimming and costs ₹999 " + "word " * 50,
        )
    )
    assert isinstance(verdict, Rejected)
    assert verdict.rule == "unknown_id"
