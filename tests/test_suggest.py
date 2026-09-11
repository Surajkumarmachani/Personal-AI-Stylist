"""Phase 6.2/6.4 — candidate generation, the request path and the lock.

The lock test is the plan's, in its own words: "run three w-cron replicas
simultaneously; assert the precompute job executes exactly once."
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from stylist_domain.context import resolve_context
from stylist_domain.scoring import ScoredGarment
from stylist_domain.taxonomy import load_taxonomy
from stylist_suggest import CandidatePool, garment_set_hash, generate_candidates, suggest


def _pool(*garments: ScoredGarment) -> CandidatePool:
    pool = CandidatePool()
    for g in garments:
        pool.by_slot.setdefault(g.slot, []).append(g)
    return pool


def g(slot: str, sub: str, **kw: object) -> ScoredGarment:
    return ScoredGarment(garment_id=f"{slot}-{sub}", slot=slot, subcategory=sub, **kw)  # type: ignore[arg-type]


# ------------------------------------------------------- set identity


def test_outfit_identity_ignores_order() -> None:
    """An outfit is a SET.

    The unique index on (user, hash, occasion, warmth) is only correct if the
    same three garments in a different order collide.
    """
    assert garment_set_hash(["a", "b", "c"]) == garment_set_hash(["c", "a", "b"])
    assert garment_set_hash(["a", "b"]) != garment_set_hash(["a", "b", "c"])


# --------------------------------------------------- candidate generation


def test_every_generated_candidate_is_rule_valid() -> None:
    """Generation must never emit an outfit the evaluator would reject.

    Scoring an invalid outfit is wasted work, and surfacing one is a bug the
    user sees.
    """
    from stylist_domain.slots import evaluate

    pool = _pool(
        g("upper_base", "shirt_oxford", formality=3, warmth=3),
        g("upper_base", "kurta", formality=3, warmth=2),
        g("lower", "chinos", formality=3, warmth=3),
        g("lower", "jeans", formality=2, warmth=3),
        g("feet", "loafers", formality=3, warmth=2),
        g("feet", "sneakers", formality=2, warmth=2),
        g("upper_layer", "blazer", formality=4, warmth=4),
        g("accessory", "belt_formal", formality=3, warmth=1),
    )
    ctx = resolve_context(occasion="office_casual", feels_like_c=26.0)
    candidates = generate_candidates(pool, ctx)
    assert candidates
    for items in candidates:
        result = evaluate([i.as_item() for i in items])
        assert result.valid, (result.violations, [(i.slot, i.subcategory) for i in items])


def test_no_candidate_contains_the_same_garment_twice() -> None:
    pool = _pool(
        g("upper_base", "shirt_oxford"),
        g("lower", "chinos"),
        g("feet", "loafers"),
        g("accessory", "watch_dress"),
    )
    ctx = resolve_context(occasion="office_casual", feels_like_c=26.0)
    for items in generate_candidates(pool, ctx):
        ids = [i.garment_id for i in items]
        assert len(ids) == len(set(ids))


def test_generation_is_deterministic_for_a_seed() -> None:
    """The precompute and the request path must agree on ordering."""
    pool = _pool(
        g("upper_base", "shirt_oxford"),
        g("upper_base", "kurta"),
        g("lower", "chinos"),
        g("feet", "loafers"),
    )
    ctx = resolve_context(occasion="office_casual", feels_like_c=26.0)
    a = [[i.garment_id for i in c] for c in generate_candidates(pool, ctx, seed=7)]
    b = [[i.garment_id for i in c] for c in generate_candidates(pool, ctx, seed=7)]
    assert a == b


def test_an_empty_wardrobe_yields_nothing_rather_than_raising() -> None:
    ctx = resolve_context(occasion="office_casual", feels_like_c=26.0)
    assert generate_candidates(CandidatePool(), ctx) == []


def test_a_wardrobe_with_no_footwear_produces_no_outfit() -> None:
    """`feet` is exactly_one in the taxonomy, so this is unsatisfiable."""
    pool = _pool(g("upper_base", "shirt_oxford"), g("lower", "chinos"))
    ctx = resolve_context(occasion="office_casual", feels_like_c=26.0)
    assert generate_candidates(pool, ctx) == []


def test_ranking_puts_the_best_score_first() -> None:
    pool = _pool(
        g("upper_base", "shirt_oxford", primary_colour="white", formality=4, warmth=3),
        g("lower", "trousers_formal", primary_colour="black", formality=4, warmth=3),
        g("upper_base", "t_shirt", primary_colour="red", formality=1, warmth=2),
        g("lower", "track_pants", primary_colour="emerald", formality=1, warmth=2),
        g("feet", "oxford_shoes", primary_colour="black", formality=4, warmth=2),
    )
    ctx = resolve_context(occasion="office_formal", feels_like_c=26.0)
    result = suggest(pool, ctx, limit=10)
    scores = [s.total for _, s in result.outfits]
    assert scores == sorted(scores, reverse=True)
    assert all(s > 0 for s in scores), "zero-scored outfits must not be surfaced"


def test_the_generated_count_is_bounded() -> None:
    """MAX_CANDIDATES is what keeps generation flat as a wardrobe grows.

    Measured: ~4ms for pools of 100, 400 and 1000 items, because the cap binds
    before pool size does.
    """
    from stylist_suggest.pipeline import MAX_CANDIDATES

    taxonomy = load_taxonomy()
    pool = CandidatePool()
    for slot in ("upper_base", "lower", "feet", "accessory"):
        for i, sub in enumerate(list(taxonomy.subcategories_by_slot[slot])[:12]):
            pool.by_slot.setdefault(slot, []).append(
                ScoredGarment(f"{slot}{i}", slot, sub, formality=3, warmth=3)
            )
    ctx = resolve_context(occasion="office_casual", feels_like_c=26.0)
    assert len(generate_candidates(pool, ctx)) <= MAX_CANDIDATES


# ------------------------------------------------------- the request path


@pytest.mark.asyncio
async def test_suggestions_rejects_an_unknown_occasion(api: AsyncClient, registered) -> None:
    resp = await api.get(
        "/suggestions", headers=registered.auth, params={"occasion": "moon_landing"}
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_suggestions_explains_an_empty_result(api: AsyncClient, registered) -> None:
    """ "No suggestions" with no reason is the least actionable screen possible.

    This tenant's single garment is still at `received` with no cutout, so the
    pool is empty — and the response has to say which slot is missing.
    """
    resp = await api.get("/suggestions", headers=registered.auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outfits"] == []
    assert body["notes"], body
    assert any("feet" in n or "base structure" in n for n in body["notes"]), body["notes"]


@pytest.mark.asyncio
async def test_suggestions_reports_the_resolved_context(api: AsyncClient, registered) -> None:
    """The caller must be able to see what was aimed at, not just the result."""
    cold = (
        await api.get(
            "/suggestions",
            headers=registered.auth,
            params={"occasion": "interview", "feels_like_c": 12.0, "wind_kmh": 40},
        )
    ).json()["context"]
    assert cold["occasion"] == "interview"
    assert cold["formality_target"] == 4
    assert cold["dress_code_target"] == "business"
    assert cold["warmth_target"] == 5
    # Already at the top of the scale, so wind CANNOT add a layer. Asserting
    # True here was my own error: there is nothing heavier than `heavy`.
    assert cold["wind_adjusted"] is False

    mild = (
        await api.get(
            "/suggestions",
            headers=registered.auth,
            params={"occasion": "interview", "feels_like_c": 24.0, "wind_kmh": 40},
        )
    ).json()["context"]
    assert mild["wind_adjusted"] is True
    assert mild["warmth_target"] > cold["warmth_target"] - 2


@pytest.mark.asyncio
async def test_suggestions_is_tenant_scoped(api: AsyncClient, registered, second_tenant) -> None:
    a = (await api.get("/suggestions", headers=registered.auth)).json()
    b = (await api.get("/suggestions", headers=second_tenant.auth)).json()
    ids_a = {g["id"] for o in a["outfits"] for g in o["garments"]}
    ids_b = {g["id"] for o in b["outfits"] for g in o["garments"]}
    assert not (ids_a & ids_b)


# ------------------------------------------------------- the distributed lock


@pytest.mark.asyncio
async def test_three_replicas_execute_the_precompute_exactly_once(api: AsyncClient) -> None:
    """The plan's test, verbatim in intent.

    try_advisory_lock, not the blocking form: a loser that queued would run the
    entire job again the moment the winner finished, turning three replicas
    into three sequential runs.
    """
    from stylist_worker.precompute import nightly_precompute

    results = await asyncio.gather(
        *[nightly_precompute({}) for _ in range(3)], return_exceptions=True
    )
    ran = [r for r in results if isinstance(r, dict) and r.get("ran")]
    skipped = [r for r in results if isinstance(r, dict) and not r.get("ran")]
    errors = [r for r in results if not isinstance(r, dict)]
    assert not errors, errors
    assert len(ran) == 1, f"expected exactly one execution, got {len(ran)}"
    assert len(skipped) == 2
    assert all(r["reason"] == "lock_held" for r in skipped)


@pytest.mark.asyncio
async def test_the_precompute_driver_can_see_tenants(
    api: AsyncClient, registered, owner_engine
) -> None:
    """Regression test for a silent no-op.

    The driver runs as `stylist_app` against FORCE-RLS tables. A direct
    `SELECT DISTINCT user_id FROM garments` returns zero rows with no error,
    and the job then reports a successful run that precomputed nothing —
    indistinguishable from a night when nobody owned any clothes. Migration
    0008 exists for this.
    """
    async with owner_engine.begin() as conn:
        uid = (
            await conn.execute(
                text("SELECT id FROM users WHERE email = :e"), {"e": registered.email}
            )
        ).scalar_one()
        await conn.execute(
            text(
                """
                INSERT INTO garments (id, user_id, original_key, state, is_active,
                                      created_at, updated_at)
                VALUES (:id, :uid, :k, 'matted', true, now(), now())
                """
            ),
            {"id": uuid.uuid4(), "uid": uid, "k": f"originals/{uid}/{uuid.uuid4()}"},
        )

    from stylist_db.session import system_session
    from stylist_worker.precompute import _tenants

    async with system_session() as session:
        tenants = await _tenants(session)
    assert uid in tenants, "the precompute driver cannot see a tenant that owns garments"
