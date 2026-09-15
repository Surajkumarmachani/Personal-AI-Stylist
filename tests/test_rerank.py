"""Phase 7.1 and 7.3 — the reranker, its fallbacks, and the rationale cache.

The exit criterion this file carries is "kill the reranker provider and
suggestions still return, non-5xx, with a template rationale". It is asserted
by making the gateway raise, not by inspection — and separately for each way it
can fail, because "the provider is down" and "the provider answered nonsense"
reach the fallback through different code.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from stylist_domain.context import OutfitContext
from stylist_domain.rerank_schema import RERANK_TOP_N, build_prompt, build_schema
from stylist_suggest.rerank import (
    CACHE_BUCKET,
    RERANK_TIMEOUT_S,
    TEMPLATE_RATIONALE,
    VALIDATOR_BUCKET,
    rationale_key,
    rerank,
)

TOP, BOTTOM, SHOES = (str(uuid.uuid4()) for _ in range(3))
TOP2, SHOES2 = (str(uuid.uuid4()) for _ in range(2))

A = (TOP, BOTTOM, SHOES)
B = (TOP2, BOTTOM, SHOES2)

SLOTS = {
    TOP: ("upper_base", "shirt"),
    TOP2: ("upper_base", "kurta"),
    BOTTOM: ("lower", "trousers"),
    SHOES: ("feet", "sneakers"),
    SHOES2: ("feet", "sandals"),
}
ITEMS = {
    gid: {"slot": slot, "subcategory": sub, "colour": "maroon"}
    for gid, (slot, sub) in SLOTS.items()
}

CTX = OutfitContext(
    warmth_target=2,
    formality_target=3,
    dress_code_target="casual",
    wet=False,
    wind_adjusted=False,
    occasion="casual_outing",
    feels_like_c=26.4,
)


class FakeGateway:
    """Records calls so "one call, no retry" is asserted by COUNT."""

    def __init__(self, *, content: str | None = None, raises: Exception | None = None):
        self.calls: list[dict[str, Any]] = []
        self._content = content
        self._raises = raises

    async def chat(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises

        class R:
            content = self._content or "{}"
            latency_ms = 640

        return R()


class FakeCache:
    def __init__(self, seed: dict[str, str] | None = None):
        self.store: dict[str, str] = dict(seed or {})
        self.counters: dict[str, dict[str, int]] = {}
        self.fail = False

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise RuntimeError("redis down")
        return self.store.get(key)

    async def set(self, key: str, value: str, *, ttl_seconds: int) -> None:
        if self.fail:
            raise RuntimeError("redis down")
        self.store[key] = value

    async def incr_bucketed(
        self, key: str, field: str, *, ttl_seconds: int, amount: int = 1
    ) -> None:
        # `amount` is honoured, not ignored. A fake that always adds 1 would
        # pass while the real counter under-reported — which is precisely the
        # bug the day-bucketed counters exist to fix.
        self.counters.setdefault(key, {}).setdefault(field, 0)
        self.counters[key][field] += amount

    def total(self, prefix: str, field: str) -> int:
        """Sum one field across day buckets, the way /ops/rerank reads them."""
        return sum(v.get(field, 0) for k, v in self.counters.items() if k.startswith(prefix))


def good_response(outfits=(A, B)) -> str:
    return json.dumps(
        {
            "outfits": [
                {
                    "garment_ids": list(o),
                    "rationale": "Breathable cotton for a warm afternoon.",
                    "confidence": 0.9,
                }
                for o in outfits
            ]
        }
    )


async def run(gateway, *, cache=None, outfits=(A, B), **kw):
    return await rerank(
        list(outfits),
        CTX,
        items_by_id=ITEMS,
        active_ids=set(SLOTS),
        slots_by_id=SLOTS,
        gateway=gateway,
        api_key="sk-tenant",
        model="outfit-reranker",
        cache=cache,
        **kw,
    )


# ------------------------------------------------------- the happy path


async def test_a_valid_response_reorders_and_attaches_rationales() -> None:
    gw = FakeGateway(content=good_response((B, A)))  # model prefers B
    out = await run(gw)

    assert out.source == "reranked"
    assert out.order == [B, A], "the model's ordering should be applied"
    assert out.rationales[B] == "Breathable cotton for a warm afternoon."
    assert out.reject_rule is None


async def test_exactly_one_call_with_a_hard_timeout_and_no_retry() -> None:
    """§7.1: 1200ms, below the 1500ms SLO, and no retry inside the request.

    `num_retries=0` is sent in the BODY because litellm/config.yaml sets
    `num_retries: 2` globally for tagging. Without the per-call override, "no
    retry" would be a comment rather than a behaviour — and two retries on a
    1200ms budget is how you turn one slow response into a breached SLO.
    """
    gw = FakeGateway(content=good_response())
    await run(gw)

    assert len(gw.calls) == 1
    assert gw.calls[0]["timeout"] == RERANK_TIMEOUT_S == 1.2
    assert gw.calls[0]["num_retries"] == 0


async def test_no_images_are_ever_sent() -> None:
    """Latency, cost and privacy, in that order. The tags ARE the extraction;
    re-sending pixels on every morning's suggestion buys nothing."""
    gw = FakeGateway(content=good_response())
    await run(gw)

    body = json.dumps(gw.calls[0]["messages"])
    assert "image_url" not in body and "base64" not in body and "data:image" not in body


async def test_only_the_top_8_are_sent_and_the_rest_keep_their_place() -> None:
    """The reranker fixes the ORDER of plausible outfits. An outfit the scorer
    ranked 12th is not one the model should be promoting to first — if it
    should, the scorer is what needs fixing."""
    many = [A, B] + [(TOP, BOTTOM, f"{SHOES}-{i}") for i in range(12)]
    gw = FakeGateway(content=good_response((A, B)))
    out = await run(gw, outfits=many)

    body = gw.calls[0]["messages"][0]["content"]
    sent = json.loads(body.split("Outfits (", 1)[1].split("\n", 1)[1].rsplit("\n\nReturn", 1)[0])
    assert len(sent) == RERANK_TOP_N
    # Nothing is lost: the 6 outfits below the top-8 keep their deterministic
    # place, and the 6 sent-but-unreturned ones are re-appended.
    assert len(out.order) == len(many)
    assert set(out.order) == set(many)


async def test_outfits_the_model_omits_are_not_silently_dropped() -> None:
    """A model that answers with 2 of the 8 it was sent is schema-valid and
    passes every §C3 assertion — the validator checks that what came back was
    sent, never that what was sent came back.

    Without this, five wearable outfits vanish from the user's list with no
    error anywhere. Found by a test whose fixture was accidentally larger than
    the model's reply, which is the only reason it was found at all.
    """
    eight = [A, B] + [(TOP, BOTTOM, f"{SHOES}-{i}") for i in range(6)]
    gw = FakeGateway(content=good_response((B,)))  # the model returns ONE
    out = await run(gw, outfits=eight)

    assert out.source == "reranked"
    assert out.order[0] == B, "the model's one opinion is still honoured"
    assert set(out.order) == set(eight), "and nothing it ignored was lost"
    assert out.rationales[A] == TEMPLATE_RATIONALE


# ------------------------------------- the exit criterion: never a 5xx


async def test_a_dead_provider_still_returns_suggestions() -> None:
    """PHASE 7 EXIT CRITERION: kill the reranker provider, suggestions still
    return, non-5xx, template rationale."""
    gw = FakeGateway(raises=ConnectionError("provider down"))
    out = await run(gw)

    assert out.source == "deterministic"
    assert out.order == [A, B], "deterministic order is preserved exactly"
    assert out.rationales[A] == TEMPLATE_RATIONALE
    assert "unavailable" in out.notes[0]


async def test_a_timeout_degrades_rather_than_raising() -> None:
    """The 1200ms timeout exists to be HIT. If hitting it raised, the deliberate
    deadline would be a 500 generator on a user-facing path."""
    import httpx

    gw = FakeGateway(raises=httpx.ReadTimeout("too slow"))
    out = await run(gw)
    assert out.source == "deterministic"


async def test_unparseable_json_falls_back_without_raising() -> None:
    gw = FakeGateway(content="not json at all{{")
    out = await run(gw)

    assert out.source == "deterministic"
    assert out.reject_rule == "schema"


async def test_a_fabricated_id_falls_back_and_counts_the_rule() -> None:
    """The phase's defining test, end to end through the orchestration:
    rejected, metric tagged `unknown_id`, user gets deterministic order."""
    bad = json.dumps(
        {
            "outfits": [
                {
                    "garment_ids": [TOP, BOTTOM, str(uuid.uuid4())],
                    "rationale": "Looks great.",
                    "confidence": 0.95,
                }
            ]
        }
    )
    cache = FakeCache()
    out = await run(FakeGateway(content=bad), cache=cache)

    assert out.source == "deterministic"
    assert out.reject_rule == "unknown_id"
    assert out.order == [A, B]
    assert cache.total(VALIDATOR_BUCKET, "rule:unknown_id") == 1
    assert cache.total(VALIDATOR_BUCKET, "rejected") == 1
    # And the denominator, without which "< 2%" is unanswerable.
    assert cache.total(VALIDATOR_BUCKET, "attempts") == 1


async def test_an_absent_reranker_is_not_an_error() -> None:
    out = await rerank(
        [A, B],
        CTX,
        items_by_id=ITEMS,
        active_ids=set(SLOTS),
        slots_by_id=SLOTS,
        gateway=None,
        api_key=None,
        model="outfit-reranker",
    )
    assert out.source == "deterministic"
    assert out.rationales[A] == TEMPLATE_RATIONALE


# ------------------------------------------------- 7.3 the rationale cache


def test_the_cache_key_buckets_temperature_and_occasion() -> None:
    """Raw values give a 0% hit rate — 26.3°C and 26.4°C are the same outfit
    and would be different keys. The buckets are the product's own
    (`warmth_target`, `dress_code_target`), not a second invented scheme that
    could disagree with what the outfit was selected for."""
    warm = OutfitContext(2, 3, "casual", False, False, "casual_outing", 26.4)
    warmer = OutfitContext(2, 3, "casual", False, False, "casual_outing", 26.3)
    # Same warmth bucket, different raw temperature -> same key.
    assert rationale_key(A, warm) == rationale_key(A, warmer)

    # A different occasion that maps to the same dress code also collides,
    # which is the point: 18 occasions collapse to 8 dress codes.
    other_occasion = OutfitContext(2, 3, "casual", False, False, "errands", 26.4)
    assert rationale_key(A, other_occasion) == rationale_key(A, warm)

    # But rain, warmth and the garment set each change it.
    assert rationale_key(A, OutfitContext(2, 3, "casual", True, False, "x", 26.4)) != rationale_key(
        A, warm
    )
    assert rationale_key(A, OutfitContext(4, 3, "casual", False, False, "x", 8.0)) != rationale_key(
        A, warm
    )
    assert rationale_key(B, warm) != rationale_key(A, warm)


def test_the_key_is_order_independent() -> None:
    """The same three garments in any order are one outfit; fragmenting the
    cache on tuple order would quietly halve the hit rate."""
    assert rationale_key((TOP, BOTTOM, SHOES), CTX) == rationale_key((SHOES, TOP, BOTTOM), CTX)


async def test_a_full_cache_hit_makes_no_provider_call() -> None:
    cache = FakeCache(
        {
            rationale_key(A, CTX): "Cached reason A.",
            rationale_key(B, CTX): "Cached reason B.",
        }
    )
    gw = FakeGateway(content=good_response())
    out = await run(gw, cache=cache)

    assert gw.calls == [], "a full hit must not reach the provider"
    assert out.source == "cache"
    assert out.rationales[A] == "Cached reason A."
    assert out.cache_hits == 2 and out.cache_misses == 0
    assert cache.total(CACHE_BUCKET, "hit") == 2, "counted per LOOKUP, not per request"


async def test_a_partial_cache_hit_still_skips_the_provider_on_the_request_path() -> None:
    """The model returns rationales for only some of the outfits it is sent —
    ~3 of 8, measured — so a context is never FULLY cached.

    Requiring a full hit therefore meant every request missed and every request
    made a live call the 1200ms budget cannot afford. It only looked fast
    because LiteLLM's own response cache was absorbing the repeat. Outfits with
    no cached rationale get the template; a caption is worth far less than not
    spending 3-6s of someone's morning.
    """
    cache = FakeCache({rationale_key(A, CTX): "Cached reason A."})
    gw = FakeGateway(content=good_response())
    out = await run(gw, cache=cache)

    assert gw.calls == [], "one hit is enough to know this context was precomputed"
    assert out.source == "cache"
    assert out.rationales[A] == "Cached reason A."
    assert out.rationales[B] == TEMPLATE_RATIONALE
    assert out.cache_hits == 1 and out.cache_misses == 1


async def test_the_nightly_path_fills_gaps_instead_of_stopping_at_the_first_hit() -> None:
    """`serve_partial_cache=False` is what the precompute passes.

    If the nightly job also stopped at the first hit, outfits the model skipped
    on the first run would never acquire a rationale and coverage would freeze
    permanently at whatever that run happened to produce.
    """
    cache = FakeCache({rationale_key(A, CTX): "Cached reason A."})
    gw = FakeGateway(content=good_response())
    out = await run(gw, cache=cache, serve_partial_cache=False)

    assert len(gw.calls) == 1, "a partial cache must not stop the nightly warm"
    assert out.source == "reranked"
    assert cache.store[rationale_key(B, CTX)], "the gap is now filled"


async def test_rationales_are_written_back_after_a_live_rerank() -> None:
    cache = FakeCache()
    await run(FakeGateway(content=good_response()), cache=cache)

    assert cache.store[rationale_key(A, CTX)] == "Breathable cotton for a warm afternoon."
    assert cache.total(CACHE_BUCKET, "miss") == 2, "both outfits missed"


async def test_a_dead_cache_does_not_break_the_reranker() -> None:
    """A cache is an optimisation. A suggestion endpoint that 500s because
    Redis is down has made its cache a dependency."""
    cache = FakeCache()
    cache.fail = True
    out = await run(FakeGateway(content=good_response()), cache=cache)

    assert out.source == "reranked"


# ------------------------------------------------ assertion 6, end to end


async def test_low_confidence_outfits_are_demoted_below_the_rest() -> None:
    content = json.dumps(
        {
            "outfits": [
                {"garment_ids": list(B), "rationale": "Not sure.", "confidence": 0.2},
                {"garment_ids": list(A), "rationale": "Good for the heat.", "confidence": 0.9},
            ]
        }
    )
    out = await run(FakeGateway(content=content))

    assert out.source == "reranked"
    assert out.order == [A, B], "the 0.2-confidence outfit sinks below the confident one"
    assert out.rationales[B] == TEMPLATE_RATIONALE, "a demoted outfit keeps no model text"


# --------------------------------------------------------- schema/prompt


def test_the_schema_is_strict_and_requires_every_field() -> None:
    schema = build_schema()["json_schema"]
    assert schema["strict"] is True
    item = schema["schema"]["properties"]["outfits"]["items"]
    assert set(item["required"]) == {"garment_ids", "rationale", "confidence"}
    assert set(item["required"]) == set(item["properties"])
    assert item["additionalProperties"] is False


def test_the_prompt_states_the_task_rather_than_pleading() -> None:
    """The validator enforces; the prompt explains. A prompt that begs the
    model not to invent garments is doing the gate's job badly."""
    prompt = build_prompt(
        [{"garment_ids": list(A), "items": [ITEMS[g] for g in A]}],
        context={"occasion": "casual_outing"},
    )
    assert "casual_outing" in prompt
    assert TOP in prompt
