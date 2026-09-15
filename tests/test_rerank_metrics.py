"""Phase 7's two rate criteria, and whether they can be measured at all.

This file exists because the first version of these counters could not answer
their own questions, in two different ways:

  - `validator.reject` counted ONLY rejections, so it could report a tally and
    never a RATE — and "< 2% overall" is a rate.
  - the cache SLI incremented `hit` and `miss` once per REQUEST, so a request
    looking up 5 outfits and finding 1 reported 50% against a true 20%,
    landing exactly on the threshold the alert fires at.

Both are the shape this project has now hit four times: a signal that is live,
green, and structurally incapable of answering the question it was built for.
So every test here asserts the FIRING direction and a correct RATE, not merely
that a counter moved.
"""

from __future__ import annotations

import time

from stylist_api.routers.ops import (
    MIN_SAMPLE_FOR_RATE,
    _rationale_cache_hit_rate,
    _rerank_metrics,
    _validator_reject_rate,
)
from stylist_suggest.rerank import CACHE_BUCKET, VALIDATOR_BUCKET, metric_keys


class FakeCache:
    """Day-bucketed hashes, like CacheRedis but in a dict."""

    def __init__(self) -> None:
        self.buckets: dict[str, dict[str, int]] = {}

    async def incr_bucketed(
        self, key: str, field: str, *, ttl_seconds: int, amount: int = 1
    ) -> None:
        self.buckets.setdefault(key, {}).setdefault(field, 0)
        self.buckets[key][field] += amount

    async def read_buckets(self, keys: list[str]) -> list[dict[str, str]]:
        return [{k: str(v) for k, v in self.buckets.get(key, {}).items()} for key in keys]


# ------------------------------------------------------- key construction


def test_reader_and_writer_share_one_key_format() -> None:
    """A reader that builds its own keys is how the Phase 5 alerts ended up
    querying an empty set while reporting healthy."""
    now = time.time()
    keys = metric_keys(CACHE_BUCKET, days=7, now=now)
    assert len(keys) == 7
    assert len(set(keys)) == 7, "seven distinct days"
    assert all(k.startswith(f"{CACHE_BUCKET}:") for k in keys)
    # Newest first, and today is included — a window that silently excluded
    # today would under-report every rate by a day.
    assert keys[0] == f"{CACHE_BUCKET}:{time.strftime('%Y%m%d', time.gmtime(now))}"

    # The two counters must not collide: one prefix serving both would add
    # `hit` into the same hash as `accepted` and silently merge two unrelated
    # rates into one.
    validator = metric_keys(VALIDATOR_BUCKET, days=7, now=now)
    assert set(validator).isdisjoint(keys)
    assert all(k.startswith(f"{VALIDATOR_BUCKET}:") for k in validator)


# ------------------------------------------------ 7.3 cache hit rate


async def test_the_cache_rate_counts_lookups_not_requests() -> None:
    """THE BUG THIS FILE EXISTS FOR.

    One request, five outfits, one cached. That is a 20% hit rate. Counting
    per-request scored it 1 hit / 1 miss = 50% — above the 50% floor, so the
    alert stayed silent at exactly the level it exists to catch.
    """
    from stylist_suggest.rerank import _record_cache

    cache = FakeCache()
    await _record_cache(cache, hits=1, misses=4)
    counts, _ = await _rerank_metrics(cache, 7)

    assert counts == {"hit": 1, "miss": 4}
    check = await _rationale_cache_hit_rate(counts, 7)
    assert check["rate"] == 0.2, "a 20% hit rate must read as 20%"


async def test_the_cache_alert_fires_below_the_floor() -> None:
    from stylist_suggest.rerank import _record_cache

    cache = FakeCache()
    await _record_cache(cache, hits=5, misses=45)  # 10%
    counts, _ = await _rerank_metrics(cache, 7)
    check = await _rationale_cache_hit_rate(counts, 7)

    assert check["firing"] is True
    assert check["rate"] == 0.1
    assert check["measured"] is True


async def test_the_cache_alert_is_silent_when_healthy() -> None:
    """The counterweight: an alert tuned until it always fires is noise."""
    from stylist_suggest.rerank import _record_cache

    cache = FakeCache()
    await _record_cache(cache, hits=45, misses=5)  # 90%
    counts, _ = await _rerank_metrics(cache, 7)
    assert (await _rationale_cache_hit_rate(counts, 7))["firing"] is False


# --------------------------------------------- §C3 validator reject rate


async def test_the_reject_rate_has_a_denominator() -> None:
    """Counting only rejections gives a tally, never a rate.

    "< 2% overall" is unanswerable without the accepted side, and the first
    version of this recorded only rejections — so the criterion could not have
    been evaluated at all, however much traffic arrived.
    """
    from stylist_suggest.rerank import _record_accept, _record_reject

    cache = FakeCache()
    for _ in range(99):
        await _record_accept(cache)
    await _record_reject(cache, "unknown_id")

    _, counts = await _rerank_metrics(cache, 7)
    assert counts["attempts"] == 100
    assert counts["accepted"] == 99
    assert counts["rejected"] == 1

    check = await _validator_reject_rate(counts, 7)
    assert check["rate"] == 0.01, "1 rejection in 100 attempts is 1%"
    assert check["firing"] is False, "1% is under the 2% ceiling"


async def test_the_reject_alert_fires_above_the_ceiling_and_names_the_rule() -> None:
    """The breakdown is the point: `unknown_id` climbing means the prompt or
    model regressed, `inactive` means the precompute is stale. One number
    cannot tell them apart and the response differs."""
    from stylist_suggest.rerank import _record_accept, _record_reject

    cache = FakeCache()
    for _ in range(90):
        await _record_accept(cache)
    for _ in range(8):
        await _record_reject(cache, "unknown_id")
    for _ in range(2):
        await _record_reject(cache, "inactive")

    _, counts = await _rerank_metrics(cache, 7)
    check = await _validator_reject_rate(counts, 7)

    assert check["rate"] == 0.1, "10 rejections in 100 attempts"
    assert check["firing"] is True
    assert check["by_rule"] == {"unknown_id": 8, "inactive": 2}


# ------------------------------------------- the under-sampled guard


async def test_a_rate_over_too_few_samples_never_fires_and_says_so() -> None:
    """2 failures in 3 is 67% and means nothing. But `measured: False` must be
    reported rather than presented as a pass — letting "not measured" become
    "measured and fine" is the failure the eval harness exits 2 to avoid."""
    from stylist_suggest.rerank import _record_cache, _record_reject

    cache = FakeCache()
    await _record_cache(cache, hits=0, misses=3)  # a 0% hit rate, on 3 lookups
    await _record_reject(cache, "schema")

    cache_counts, validator_counts = await _rerank_metrics(cache, 7)
    hit = await _rationale_cache_hit_rate(cache_counts, 7)
    rej = await _validator_reject_rate(validator_counts, 7)

    assert hit["lookups"] < MIN_SAMPLE_FOR_RATE
    assert hit["firing"] is False and hit["measured"] is False
    assert hit["rate"] == 0.0, "the rate is still reported; only the ALERT is suppressed"
    assert rej["firing"] is False and rej["measured"] is False


async def test_an_empty_window_reports_unmeasured_rather_than_healthy() -> None:
    """No traffic at all. `rate: None`, not 0 or 1 — a cache nobody used has no
    hit rate, and inventing one would make a cold start look like an outage or
    a success depending on which default was picked."""
    cache = FakeCache()
    cache_counts, validator_counts = await _rerank_metrics(cache, 7)

    hit = await _rationale_cache_hit_rate(cache_counts, 7)
    rej = await _validator_reject_rate(validator_counts, 7)
    assert hit["rate"] is None and hit["measured"] is False and hit["firing"] is False
    assert rej["rate"] is None and rej["measured"] is False and rej["firing"] is False


async def test_counters_survive_a_dead_cache() -> None:
    """Telemetry must never fail the request it measures."""
    from stylist_suggest.rerank import _record_accept, _record_cache, _record_reject

    class Broken(FakeCache):
        async def incr_bucketed(self, *a: object, **kw: object) -> None:
            raise RuntimeError("redis down")

    broken = Broken()
    await _record_cache(broken, hits=1, misses=1)
    await _record_reject(broken, "schema")
    await _record_accept(broken)  # no exception is the assertion
