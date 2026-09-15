"""Reranker orchestration: cache -> call -> validate -> fall back (Steps 7.1, 7.3).

THIS MODULE CANNOT FAIL THE REQUEST
-----------------------------------
`rerank()` returns a `RerankOutcome` for every input, including one where the
provider is down, the budget is gone, the response is garbage, or the model
invented a garment. The deterministic ranking is always available because it is
what came IN, so every failure path is "return what we already had, with
template rationales". Nothing here raises, and the one exit criterion that
matters — "kill the reranker provider, suggestions still return, non-5xx" — is
a property of that shape rather than of an exception handler somewhere.

THE RATIONALE CACHE, AND WHY ITS KEY IS THE PRODUCT'S OWN BUCKETS
-----------------------------------------------------------------
§7.3 keys on `(garment_set_hash, occasion_bucket, temp_bucket, precip_bool)` and
warns that raw temperature gives a 0% hit rate — 26.3°C and 26.4°C are the same
outfit and a different key.

Rather than invent a bucketing, this uses the buckets the product already has:
`dress_code_target` (18 occasions collapse to 8 dress codes) and
`warmth_target` (1-5, from `taxonomy.yaml`'s `feels_like_c_max` ceilings). Both
are already on `OutfitContext` because the candidate generator needed them. The
gain is not brevity: a separately-invented temperature bucket could disagree
with the warmth level the outfit was actually SELECTED for, and then the cache
would serve a rationale written for a different kind of day.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from stylist_domain.context import OutfitContext
from stylist_domain.rerank_schema import RERANK_TOP_N, build_prompt, build_schema
from stylist_domain.validator import Accepted, Rejected, validate

logger = logging.getLogger(__name__)

# §B1 gives /suggestions 1500ms. The reranker gets 1200ms so there is room left
# for the DB read, the scorer and serialisation — and so that blowing the
# budget degrades instead of breaching the SLO.
#
# MEASURED, AND IT IS NOT ENOUGH: a top-8 rerank against gemini-3.6-flash takes
# 3.2-6.4s. Cutting the response to the plan's own token budget (1085 in / 370
# out) still floors at ~3.2s, because that is generation time and not reasoning
# overhead. On the request path this timeout is therefore a DEGRADE SWITCH, not
# a deadline the call is expected to meet — and that is deliberate: the live
# path exists so a user who picks an un-precomputed context gets an answer in
# 1.3s, not so it usually reranks.
REQUEST_TIMEOUT_S = 1.2

# The nightly precompute has nobody waiting on it, so it gets time to actually
# succeed. This is where reranking really happens (see
# `stylist_worker.precompute`); the request path reads what this wrote.
PRECOMPUTE_TIMEOUT_S = 30.0

# Kept as the old name so callers that never cared about the distinction still
# read as before.
RERANK_TIMEOUT_S = REQUEST_TIMEOUT_S

# Seven days. A rationale describes clothes against weather and an occasion,
# none of which go stale quickly; the garment set hash changes the key the
# moment the wardrobe does.
RATIONALE_TTL_SECONDS = 7 * 24 * 3600

# What a user sees when the reranker did not run or was rejected. Deliberately
# plain: a template that tries to sound like the model produces a confident
# sentence about clothes nothing actually looked at.
TEMPLATE_RATIONALE = "Picked for today's weather and occasion."


class _Gateway(Protocol):
    """The slice of LiteLLMClient this module uses.

    Spelled out rather than `**kwargs: Any` because a protocol that accepts
    anything is not a contract — it would structurally match any object with a
    `chat` method, including one whose signature has drifted away from this
    call site, and the mismatch would surface at runtime in a request instead
    of at typecheck time.
    """

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        api_key: str,
        response_format: dict[str, Any] | None = ...,
        max_tokens: int = ...,
        timeout: float | None = ...,
        num_retries: int | None = ...,
    ) -> Any: ...


class _Cache(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, *, ttl_seconds: int) -> None: ...
    async def incr_bucketed(
        self, key: str, field: str, *, ttl_seconds: int, amount: int = ...
    ) -> None: ...


@dataclass
class RerankOutcome:
    """What the endpoint needs to answer, and to explain itself.

    `order` is the final outfit ordering. `source` says how it was produced —
    surfaced in the response because a user-visible ranking that silently
    switches between a model and a scorer is unexplainable when someone asks
    why today's suggestion changed.
    """

    order: list[tuple[str, ...]]
    rationales: dict[tuple[str, ...], str]
    source: str  # "reranked" | "cache" | "deterministic"
    reject_rule: str | None = None
    # Rationales actually AUTHORED BY THE MODEL and written to the cache.
    # Distinct from `len(rationales)`, which also counts the template text
    # attached to every outfit below the top-8 — counting those as work done
    # would report 200 warmed rationales for 8 real ones, and the nightly job's
    # log line is how anyone would know whether the warm is working at all.
    cached_writes: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    latency_ms: int | None = None
    notes: list[str] = field(default_factory=list)


def rationale_key(outfit_ids: tuple[str, ...], ctx: OutfitContext) -> str:
    """§7.3's key. Sorted ids, so outfit order cannot fragment the cache."""
    joined = "|".join(sorted(outfit_ids))
    digest = hashlib.sha256(joined.encode()).hexdigest()[:32]
    return f"rationale:{digest}:{ctx.dress_code_target}:{ctx.warmth_target}:{int(ctx.wet)}"


def _fallback(
    outfits: list[tuple[str, ...]], *, rule: str | None, note: str, **kw: Any
) -> RerankOutcome:
    return RerankOutcome(
        order=list(outfits),
        rationales=dict.fromkeys(outfits, TEMPLATE_RATIONALE),
        source="deterministic",
        reject_rule=rule,
        notes=[note],
        **kw,
    )


async def rerank(
    outfits: list[tuple[str, ...]],
    ctx: OutfitContext,
    *,
    items_by_id: dict[str, dict[str, Any]],
    active_ids: set[str],
    slots_by_id: dict[str, tuple[str, str]],
    gateway: _Gateway | None,
    api_key: str | None,
    model: str,
    cache: _Cache | None = None,
    min_confidence: float = 0.5,
    timeout_s: float = REQUEST_TIMEOUT_S,
    serve_partial_cache: bool = True,
) -> RerankOutcome:
    """Reorder the scorer's top-8 and attach rationales. Never raises.

    `outfits` arrives in DETERMINISTIC order and that ordering is the fallback
    for every failure below, which is why it is never recomputed here.
    """
    if not outfits:
        return RerankOutcome(order=[], rationales={}, source="deterministic")

    top = outfits[:RERANK_TOP_N]
    rest = outfits[RERANK_TOP_N:]

    # ---- 7.3: try the cache first --------------------------------------
    cached: dict[tuple[str, ...], str] = {}
    if cache is not None:
        for outfit in top:
            try:
                hit = await cache.get(rationale_key(outfit, ctx))
            except Exception as exc:  # a cache is an optimisation, never a dependency
                logger.debug("rationale cache read failed: %s", exc)
                break
            if hit:
                cached[outfit] = hit

    hits, misses = len(cached), len(top) - len(cached)
    await _record_cache(cache, hits=hits, misses=misses)

    # ANY hit means this context was precomputed, and that is enough to skip the
    # provider. It does NOT have to be a full hit.
    #
    # Requiring `misses == 0` looked right and was wrong in practice. The model
    # returns rationales for only some of the outfits it is sent (~3 of 8,
    # measured), so the cache is never complete for a context — every request
    # therefore missed, and every request made a live call the 1200ms budget
    # cannot afford. It only LOOKED fast because LiteLLM's own response cache
    # was absorbing the repeat: a 7-day gateway TTL we do not control, propping
    # up a design that was quietly not working.
    #
    # Outfits with no cached rationale fall back to the template. A caption is
    # worth far less than the ranking, and spending 3-6s of someone's morning
    # to fill in three missing captions is the wrong trade.
    #
    # The ORDER on this path stays deterministic either way: the cache stores
    # what the model said about each outfit on its own, never how it ranked
    # them against each other, so reconstructing a ranking from per-outfit text
    # would be inventing an ordering nothing produced.
    # `serve_partial_cache` is what separates the two callers, and they need
    # opposite policies from the same function. The REQUEST path takes any hit
    # and leaves the gaps as templates, because it cannot afford a 3-6s call.
    # The NIGHTLY path passes False: if it also stopped at the first hit, the
    # outfits the model skipped last night would never get a rationale, and
    # coverage would freeze permanently at whatever the first run happened to
    # produce.
    if cached and (serve_partial_cache or misses == 0):
        return RerankOutcome(
            order=list(top) + list(rest),
            rationales={
                **dict.fromkeys(top, TEMPLATE_RATIONALE),
                **cached,
                **dict.fromkeys(rest, TEMPLATE_RATIONALE),
            },
            source="cache",
            cached_writes=0,
            cache_hits=hits,
            # The REAL miss count, not 0. This path can now be reached with
            # gaps, and reporting them as hits would flatter the §7.3 hit-rate
            # SLI with the one number it exists to detect.
            cache_misses=misses,
        )

    if gateway is None or api_key is None:
        return _fallback(
            outfits,
            rule=None,
            note="reranker not configured; deterministic order",
            cache_hits=hits,
            cache_misses=misses,
        )

    # ---- 7.1: one call, 1200ms, no retry -------------------------------
    payload = [
        {
            "garment_ids": list(outfit),
            "items": [items_by_id[g] for g in outfit if g in items_by_id],
        }
        for outfit in top
    ]
    context = {
        "occasion": ctx.occasion,
        "dress_code": ctx.dress_code_target,
        "formality_target": ctx.formality_target,
        "warmth_target": ctx.warmth_target,
        "feels_like_c": round(ctx.feels_like_c, 1),
        "wet": ctx.wet,
    }

    try:
        result = await gateway.chat(
            model=model,
            messages=[{"role": "user", "content": build_prompt(payload, context=context)}],
            api_key=api_key,
            response_format=build_schema(),
            # 4096, NOT 1024, AND THE REASON IS THE UUIDS.
            #
            # §7.1 budgets "~300 out", which is right for the rationales and
            # wrong for the response: the model must echo the garment ids, and
            # a uuid costs ~22 tokens. Eight outfits of three garments is ~530
            # tokens of pure id before a single word of rationale, so a
            # 1024-cap truncates the JSON mid-object and the whole rerank is
            # discarded as unparseable — which presents as the MODEL failing
            # rather than the ceiling being too low.
            #
            # This is the second time this exact failure has appeared in this
            # project; litellm/config.yaml carries the same note for tagging at
            # 2048. Echoing ids is what makes it expensive, and referencing
            # outfits by index instead would cut the output to ~370 tokens —
            # measured, and recorded in the plan as the follow-up.
            max_tokens=4096,
            timeout=timeout_s,
            # §7.1: NO RETRY INSIDE THE REQUEST. Overridden per call because
            # litellm/config.yaml sets num_retries globally for tagging.
            num_retries=0,
        )
    except Exception as exc:
        # Provider down, budget exhausted, timeout — all the same to the user,
        # who gets the deterministic ranking either way. Distinguishing them
        # here would only change which log line is written.
        logger.info("rerank unavailable (%s); serving deterministic order", type(exc).__name__)
        return _fallback(
            outfits,
            rule=None,
            note=f"reranker unavailable: {type(exc).__name__}",
            cache_hits=hits,
            cache_misses=misses,
        )

    latency = getattr(result, "latency_ms", None)

    try:
        parsed = json.loads(result.content)
    except (json.JSONDecodeError, TypeError):
        parsed = None

    verdict = validate(
        parsed,
        input_outfits=top,
        active_ids=active_ids,
        slots_by_id=slots_by_id,
        min_confidence=min_confidence,
    )

    if isinstance(verdict, Rejected):
        # §C3: every rejection is a metric tagged by rule. This is the earliest
        # and cheapest quality signal in the system — `unknown_id` trending up
        # means the prompt or the model regressed, and it fires before any user
        # complains.
        await _record_reject(cache, verdict.rule)
        logger.warning("validator rejected rerank: rule=%s %s", verdict.rule, verdict.detail)
        return _fallback(
            outfits,
            rule=verdict.rule,
            note=f"rerank rejected ({verdict.rule}); deterministic order",
            latency_ms=latency,
            cache_hits=hits,
            cache_misses=misses,
        )

    assert isinstance(verdict, Accepted)
    # The denominator for §7's "`validator.reject` rate < 2%". Recording only
    # rejections gives a tally, never a rate — the criterion is unanswerable
    # without this line.
    await _record_accept(cache)

    # Accepted order, then anything the model demoted for low confidence, then
    # the outfits below the top-8 that were never sent. Demoted outfits sit
    # BELOW deterministic order per §C3 assertion 6 — kept, not trusted.
    #
    # `dropped` is the subtle one. The validator checks that every outfit the
    # model RETURNED was one we sent; it says nothing about the reverse. A
    # model that answers with three of the eight it was given is schema-valid,
    # passes every assertion, and would silently delete five wearable outfits
    # from the user's list — a quiet loss with no error anywhere, which is
    # exactly the shape of failure this project keeps finding. Anything the
    # model omitted is appended in its deterministic position instead.
    returned = set(verdict.order) | set(verdict.demoted)
    dropped = [o for o in top if o not in returned]
    ordering = verdict.order + verdict.demoted + dropped + list(rest)
    rationales = {o: verdict.rationales.get(o, TEMPLATE_RATIONALE) for o in ordering}

    written_to_cache = 0
    if cache is not None:
        for outfit, text in verdict.rationales.items():
            try:
                await cache.set(rationale_key(outfit, ctx), text, ttl_seconds=RATIONALE_TTL_SECONDS)
                written_to_cache += 1
            except Exception as exc:
                logger.debug("rationale cache write failed: %s", exc)
                break

    return RerankOutcome(
        order=ordering,
        rationales=rationales,
        source="reranked",
        latency_ms=latency,
        cached_writes=written_to_cache,
        cache_hits=hits,
        cache_misses=misses,
    )


# ---------------------------------------------------------------- metrics
#
# Counters go through CacheRedis's own methods, never `.pipeline()` on the
# wrapper. That exact mistake made the Phase 5 api_5xx counter permanently
# empty: the wrapper has no such method, and the middleware swallows telemetry
# exceptions by design, so the failure was invisible and the alert could not
# fire. Telemetry must not be able to fail a request — and must not be able to
# fail silently forever either, which is what the tests pin.
#
# TWO THINGS THE FIRST VERSION OF THIS GOT WRONG, both the same class of bug as
# the three already recorded in this plan — a signal that looks live and cannot
# answer its own question:
#
#   1. THE RATE HAD NO DENOMINATOR. Only rejections were counted, so
#      `validator.reject` could report how many rejections happened and never
#      the RATE, which is what §7's "< 2% overall" criterion asks for. Every
#      validation is now counted, accepted ones included.
#   2. THE CACHE SLI COUNTED REQUESTS, NOT LOOKUPS. One request looking up 5
#      outfits and finding 1 incremented `hit` once and `miss` once: a true 20%
#      hit rate read as 50%, exactly at the threshold §7.3 alerts on. Both are
#      now incremented BY COUNT.
#
# BUCKETED BY DAY, following the middleware's per-minute pattern at the
# granularity these criteria are written in ("after a week", "overall"). A
# single accumulating key cannot express a windowed rate and never recovers
# from a bad period, because every write refreshes its own TTL.

# 14 days, so a 7-day window is always fully covered even when a bucket is
# written early in the window and not touched again.
_METRIC_TTL_SECONDS = 14 * 24 * 3600

CACHE_BUCKET = "rerank:cache"
VALIDATOR_BUCKET = "rerank:validator"


def _day(ts: float | None = None) -> str:
    return time.strftime("%Y%m%d", time.gmtime(ts if ts is not None else time.time()))


def metric_keys(prefix: str, *, days: int, now: float | None = None) -> list[str]:
    """The bucket keys covering the last `days` days, newest first.

    Shared by the writer here and the reader in `/ops/rerank` so the two cannot
    disagree about the key format — the failure mode that made the Phase 5
    alerts read an empty set while looking perfectly healthy.
    """
    base = now if now is not None else time.time()
    return [f"{prefix}:{_day(base - i * 86400)}" for i in range(days)]


async def _record_reject(cache: _Cache | None, rule: str) -> None:
    """One rejection: the rule, and the attempt it belongs to."""
    if cache is None:
        return
    key = f"{VALIDATOR_BUCKET}:{_day()}"
    try:
        await cache.incr_bucketed(key, "attempts", ttl_seconds=_METRIC_TTL_SECONDS)
        await cache.incr_bucketed(key, "rejected", ttl_seconds=_METRIC_TTL_SECONDS)
        await cache.incr_bucketed(key, f"rule:{rule}", ttl_seconds=_METRIC_TTL_SECONDS)
    except Exception as exc:
        logger.debug("validator reject counter failed: %s", exc)


async def _record_accept(cache: _Cache | None) -> None:
    """The denominator. Without this there is no rate, only a tally."""
    if cache is None:
        return
    key = f"{VALIDATOR_BUCKET}:{_day()}"
    try:
        await cache.incr_bucketed(key, "attempts", ttl_seconds=_METRIC_TTL_SECONDS)
        await cache.incr_bucketed(key, "accepted", ttl_seconds=_METRIC_TTL_SECONDS)
    except Exception as exc:
        logger.debug("validator accept counter failed: %s", exc)


async def _record_cache(cache: _Cache | None, *, hits: int, misses: int) -> None:
    """The §7.3 hit-rate SLI, which alerts below 50%.

    Incremented BY COUNT, not by one per request — see the note above. Kept as
    two counters rather than a stored ratio so a rate over too few samples can
    be refused at read time, the guard Phase 5 added after learning that "2
    failures in 3" is 67% and means nothing.
    """
    if cache is None or (hits == 0 and misses == 0):
        return
    key = f"{CACHE_BUCKET}:{_day()}"
    try:
        if hits:
            await cache.incr_bucketed(key, "hit", ttl_seconds=_METRIC_TTL_SECONDS, amount=hits)
        if misses:
            await cache.incr_bucketed(key, "miss", ttl_seconds=_METRIC_TTL_SECONDS, amount=misses)
    except Exception as exc:
        logger.debug("rationale cache counter failed: %s", exc)
