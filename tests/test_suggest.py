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


def test_a_wardrobe_with_no_footwear_still_produces_an_outfit() -> None:
    """`feet` is `prefer_one`, not `exactly_one`.

    THIS TEST ASSERTED THE OPPOSITE, and the old behaviour was defensible
    about dressing and wrong about software: a wardrobe with no catalogued
    footwear produced ZERO outfits, not a worse ranking — nothing at all, with
    "no wearable feet" as the only thing on screen. Someone who has
    photographed six shirts and no shoes has told us plenty, and answering
    with an empty screen teaches them the product does not work.

    The pool is unchanged from the original test; only the expectation moved.
    """
    pool = _pool(g("upper_base", "shirt_oxford"), g("lower", "chinos"))
    ctx = resolve_context(occasion="office_casual", feels_like_c=26.0)
    candidates = generate_candidates(pool, ctx)
    assert candidates, "a shirt and trousers is a wearable outfit"
    assert all(
        not any(item.slot == "feet" for item in combo) for combo in candidates
    ), "there is no footwear to include"


def test_footwear_is_included_whenever_the_wardrobe_has_any() -> None:
    """The other half, and the reason `feet` is PREFERRED rather than optional.

    Plain `at_most_one` would let the generator produce shoeless variants
    alongside shod ones for someone who owns shoes — and a 3-piece outfit can
    outscore a 4-piece one, so the shoeless variant could rank first. Nobody
    with shoes in their wardrobe should be shown an outfit without them.
    """
    pool = _pool(
        g("upper_base", "shirt_oxford"),
        g("lower", "chinos"),
        g("feet", "oxford_shoes"),
    )
    ctx = resolve_context(occasion="office_casual", feels_like_c=26.0)
    candidates = generate_candidates(pool, ctx)
    assert candidates
    assert all(
        any(item.slot == "feet" for item in combo) for combo in candidates
    ), "every candidate must be shod when footwear exists"


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
    pool is empty — and the response has to say what is missing IN WORDS.

    The note is the entire answer the chat shows for an undressable occasion,
    so the bar is what a user can act on, not what a developer can decode. It
    used to read "no complete base structure available (upper_base+lower or
    full_body)", which named the slots in the schema's vocabulary rather than
    the wearer's; the raw-id assertion below is what stops that returning.
    """
    resp = await api.get("/suggestions", headers=registered.auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outfits"] == []
    assert body["notes"], body
    assert any(
        "shoes" in n or "wardrobe" in n or "outfit" in n for n in body["notes"]
    ), body["notes"]
    jargon = ("upper_base", "full_body", "base structure", "no wearable feet")
    for note in body["notes"]:
        assert not any(j in note for j in jargon), f"slot ids leaked to the user: {note!r}"


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


# ------------------------------------------------------------- diversify
#
# `score_outfit` judges one outfit at a time, so nothing in it can notice that
# ranks 1 and 3 are the same outfit with an anklet added. Measured on the demo
# wardrobe before this existed: six suggestions drew on SEVEN distinct
# garments across 21 slots, one garment appeared five times, and the top three
# had a mean pairwise Jaccard of 0.450. After: 12 distinct garments, 0.122.


def _fake(ids: list[str], total: float):
    """A (garments, score) pair shaped like `suggest` produces."""
    from stylist_domain.scoring import OutfitScore, ScoredGarment

    items = tuple(
        ScoredGarment(
            garment_id=gid,
            slot="upper_base",
            subcategory="shirt_casual",
            primary_colour="black",
            secondary_colour=None,
            pattern=None,
            material="cotton",
            formality=2,
            warmth=2,
            wear_count=0,
            last_worn=None,
        )
        for gid in ids
    )
    return items, OutfitScore(total=total, breakdown={})


def test_a_near_duplicate_loses_to_a_slightly_worse_but_different_outfit() -> None:
    from stylist_suggest.pipeline import diversify

    scored = [
        _fake(["a", "b", "c"], 0.90),
        _fake(["a", "b", "d"], 0.88),  # rank 2 by score, 2/3 shared with rank 1
        _fake(["x", "y", "z"], 0.80),  # much worse, completely different
    ]
    got = diversify(scored, limit=2, overlap_penalty=0.15)
    picked = [sorted(g.garment_id for g in items) for items, _ in got]

    assert picked[0] == ["a", "b", "c"], "the best outfit is always kept"
    assert picked[1] == ["x", "y", "z"], (
        "0.88 - 0.15*(2/3) = 0.78 loses to 0.80; the different outfit wins"
    )


def test_a_much_better_outfit_still_wins_despite_repeating_a_garment() -> None:
    """Variety must not be bought with quality. The penalty is a nudge, not a
    veto — an outfit that is clearly better is still shown even if it repeats."""
    from stylist_suggest.pipeline import diversify

    scored = [
        _fake(["a", "b", "c"], 0.90),
        _fake(["a", "b", "c2"], 0.89),  # near-duplicate but nearly as good
        _fake(["x", "y", "z"], 0.60),  # different and much worse
    ]
    got = diversify(scored, limit=2, overlap_penalty=0.15)
    picked = [sorted(g.garment_id for g in items) for items, _ in got]
    assert picked[1] == ["a", "b", "c2"], "0.89 - 0.10 = 0.79 still beats 0.60"


def test_a_penalty_of_zero_is_exactly_the_old_behaviour() -> None:
    """`enabled: false` in scoring.yaml has to restore the pre-Phase-12 order
    bit for bit — that is what the measured latency figures and the blind eval
    were run against, and a config flag that only approximately reverts is not
    a way back."""
    from stylist_suggest.pipeline import diversify

    scored = [_fake(["a", "b"], 0.9), _fake(["a", "c"], 0.8), _fake(["d", "e"], 0.7)]
    assert diversify(scored, limit=3, overlap_penalty=0.0) == scored[:3]


def test_diversify_is_deterministic() -> None:
    """The nightly precompute and the request path must agree, or
    `served_from: materialised` and `served_from: live` rank differently for
    the same wardrobe — the exact drift the precompute design exists to avoid."""
    from stylist_suggest.pipeline import diversify

    scored = [
        _fake(["a", "b"], 0.80),
        _fake(["a", "c"], 0.80),  # an exact score tie, broken on the set hash
        _fake(["d", "e"], 0.80),
    ]
    runs = [
        [sorted(g.garment_id for g in items) for items, _ in diversify(
            scored, limit=3, overlap_penalty=0.15
        )]
        for _ in range(5)
    ]
    assert all(r == runs[0] for r in runs)


def test_everything_overlapping_preserves_the_original_order() -> None:
    """The small-wardrobe case, and why this is a penalty rather than a
    per-garment appearance cap: with ten garments nearly every outfit shares
    something, and a cap would either empty the list or force genuinely bad
    outfits into it. A penalty applied equally leaves the ranking alone."""
    from stylist_suggest.pipeline import diversify

    scored = [_fake(["a", "b"], 0.9), _fake(["a", "b"], 0.8), _fake(["a", "b"], 0.7)]
    got = diversify(scored, limit=3, overlap_penalty=0.15)
    assert [s.total for _, s in got] == [0.9, 0.8, 0.7]


async def test_a_stored_outfit_that_breaks_todays_rules_is_not_served(
    api, registered, owner_engine
) -> None:
    """A precomputed outfit is a claim about garments as they were tagged.

    Correcting a slot -- or changing the rules -- can make that claim false
    underneath it. Two such rows were served as FIRST CHOICE from one
    precompute run: two pairs of jeans with no top, and jeans with shoes and
    no top. Neither is something the generator can produce; both had been
    materialised before a mis-tagged garment was corrected.

    The garments are INSERTED here rather than taken from the fixture, which
    owns a single untagged garment. An earlier version of this test asked the
    fixture for two `lower` garments and skipped when it found none -- so it
    reported success while never once exercising the guard.
    """
    import uuid as _uuid

    import sqlalchemy as sa

    async with owner_engine.begin() as conn:
        user_id = (
            await conn.execute(
                sa.text("SELECT id FROM users WHERE email = :e"), {"e": registered.email}
            )
        ).scalar_one()

        ids = [_uuid.uuid4(), _uuid.uuid4()]
        for gid in ids:
            await conn.execute(
                sa.text(
                    "INSERT INTO garments (id, user_id, original_key, slot, subcategory,"
                    " primary_colour, dress_code, formality, warmth, attributes_raw,"
                    " field_confidence, user_verified_fields, state, needs_review,"
                    " is_active, moderation, needs_wash, created_at, updated_at)"
                    " VALUES (:id, :u, :key, CAST('lower' AS slot),"
                    " CAST('jeans' AS subcategory), CAST('denim_indigo' AS colour),"
                    " CAST('casual' AS dress_code), 2, 2, '{}'::jsonb, '{}'::jsonb,"
                    " '{}', 'matted', false, true, '{}'::jsonb, false, now(), now())"
                ),
                {"id": gid, "u": user_id, "key": f"originals/{user_id}/{gid}"},
            )

        # TWO BOTTOMS AND NOTHING ELSE: no base structure, so not an outfit.
        # Score 0.99 puts it first if it is served at all.
        await conn.execute(
            sa.text(
                "INSERT INTO outfits (id, user_id, garment_ids, garment_set_hash,"
                " occasion, warmth_target, formality_target, wet, score,"
                " score_breakdown, scoring_version, created_at) VALUES"
                " (gen_random_uuid(), :u, ARRAY[CAST(:a AS uuid), CAST(:b AS uuid)],"
                " 'impossible-outfit-hash', 'casual_outing', :w, 3, false, 0.99,"
                " '{}'::jsonb, 1, now())"
            ),
            # warmth_target is only ever 1, 3 or 5 -- `resolve_context` maps
            # every temperature into those three bands. An earlier version of
            # this test stored 2, which the serving query could never match
            # (it filters on warmth_target), so the row was never read and the
            # test passed WITH THE GUARD DISABLED.
            {"u": user_id, "a": ids[0], "b": ids[1], "w": 3},
        )

    # 26 C -> warmth band 3, matching the row above.
    resp = await api.get(
        "/suggestions?occasion=casual_outing&feels_like_c=26", headers=registered.auth
    )
    assert resp.status_code == 200, resp.text
    served = resp.json()["outfits"]

    for outfit in served:
        slots = [g["slot"] for g in outfit["garments"]]
        assert len(slots) == len(set(slots)), f"two garments in one slot: {slots}"
        assert "upper_base" in slots or "full_body" in slots, f"no top: {slots}"


@pytest.mark.asyncio
async def test_a_stored_outfit_is_rechecked_against_the_occasions_dress_code(
    api: AsyncClient, registered, owner_engine
) -> None:
    """DENIM AT THE GYM. Reported from the running app, top four looks.

    "Something for the gym" returned denim shirts, jeans and white sneakers.
    The rows were real: materialised under `occasion = 'workout'` from
    garments tagged `casual`, while `activewear` accepts only `activewear`.
    Re-running `load_wardrobe` for that wardrobe and occasion produced an
    EMPTY pool, so the live path had been right the whole time — the stored
    rows had simply outlived the tags they were built from, and `_hydrate`
    re-checked the STRUCTURE of a stored outfit but never whether it still
    suited the occasion it was filed under.

    The structure here is deliberately VALID (top, bottom, shoes) so that the
    existing slot-rule guard cannot be what rejects it. Only a dress-code
    check can, which is what makes this a test of the fix rather than of the
    guard next to it.
    """
    import uuid as _uuid

    import sqlalchemy as sa

    async with owner_engine.begin() as conn:
        user_id = (
            await conn.execute(
                sa.text("SELECT id FROM users WHERE email = :e"), {"e": registered.email}
            )
        ).scalar_one()

        planted = {
            "upper_base": (_uuid.uuid4(), "shirt_casual"),
            "lower": (_uuid.uuid4(), "jeans"),
            "feet": (_uuid.uuid4(), "sneakers"),
        }
        for slot, (gid, sub) in planted.items():
            await conn.execute(
                sa.text(
                    "INSERT INTO garments (id, user_id, original_key, slot, subcategory,"
                    " primary_colour, dress_code, formality, warmth, attributes_raw,"
                    " field_confidence, user_verified_fields, state, needs_review,"
                    " is_active, moderation, needs_wash, created_at, updated_at)"
                    f" VALUES (:id, :u, :key, CAST('{slot}' AS slot),"
                    f" CAST('{sub}' AS subcategory), CAST('denim_indigo' AS colour),"
                    " CAST('casual' AS dress_code), 2, 3, '{}'::jsonb, '{}'::jsonb,"
                    " '{}', 'matted', false, true, '{}'::jsonb, false, now(), now())"
                ),
                {"id": gid, "u": user_id, "key": f"originals/{user_id}/{gid}"},
            )

        # Score 0.99 puts it first if it is served at all.
        await conn.execute(
            sa.text(
                "INSERT INTO outfits (id, user_id, garment_ids, garment_set_hash,"
                " occasion, warmth_target, formality_target, wet, score,"
                " score_breakdown, scoring_version, created_at) VALUES"
                " (gen_random_uuid(), :u, ARRAY[CAST(:a AS uuid), CAST(:b AS uuid),"
                " CAST(:c AS uuid)], 'denim-at-the-gym-hash', 'workout', :w, 1,"
                " false, 0.99, '{}'::jsonb, 1, now())"
            ),
            {
                "u": user_id,
                "a": planted["upper_base"][0],
                "b": planted["lower"][0],
                "c": planted["feet"][0],
                # 26 C -> warmth band 3, which the serving query filters on.
                "w": 3,
            },
        )

    resp = await api.get(
        "/suggestions?occasion=workout&feels_like_c=26", headers=registered.auth
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    served = {g["id"] for o in body["outfits"] for g in o["garments"]}
    for slot, (gid, sub) in planted.items():
        assert str(gid) not in served, f"{sub} ({slot}, casual) served for a workout"

    # AND THE USER IS TOLD WHY. Dropping the row must not turn into a blank
    # screen with no reason — that is the failure the notes exist to prevent.
    if not body["outfits"]:
        assert body["notes"], "an empty result has to say what is missing"


def test_the_universal_calendar_dresses_a_user_with_no_diary() -> None:
    """Occasion resolution was: your Google calendar, else `casual_outing`.

    So on Independence Day a user who had not connected a calendar was offered
    an everyday casual look -- from information the system already had, since
    the date is not personal data and needs no integration.
    """
    import datetime as dt

    from stylist_domain.observances import observance_for

    o = observance_for(dt.date(2026, 8, 15))
    assert o is not None and o.occasion == "festival_day"
    assert observance_for(dt.date(2026, 9, 23)) is None


def test_every_observance_names_a_real_taxonomy_occasion() -> None:
    """A typo here is a 400 on the day of the festival, for every user at
    once, and only on that day."""
    import yaml

    from stylist_domain.observances import CONFIG
    from stylist_domain.taxonomy import load_taxonomy

    ids = {o["id"] for o in load_taxonomy().raw["occasions"]}
    table = yaml.safe_load(CONFIG.open())
    for row in (table.get("recurring") or []) + (table.get("dated") or []):
        assert row["occasion"] in ids, f"{row['name']} -> unknown occasion {row['occasion']}"


def test_the_calendar_admits_when_it_has_run_out() -> None:
    """Moving festivals are listed by hand and simply stop.

    Returning "no observance" for an uncovered year would make a calendar that
    has expired indistinguishable from one reporting an ordinary day -- the
    exact failure this codebase keeps turning up. `is_stale` is what lets the
    caller say so.
    """
    import datetime as dt

    from stylist_domain.observances import coverage_until, is_stale

    assert is_stale(dt.date(coverage_until().year + 2, 6, 1)) is True
    assert is_stale(coverage_until()) is False


def test_a_bank_holiday_is_not_holi() -> None:
    """The first version of the holiday mapper matched on plain substrings,
    and "holi" is inside "Bank **Holi**day".

    Every UK bank holiday would have resolved to Holi and dressed people in
    festive ethnic wear for a long weekend. Found by testing the mapper
    against names it would actually see rather than only the ones it was
    written for.
    """
    from stylist_domain.observances import occasion_for_holiday_name

    assert occasion_for_holiday_name("Holi")[1] is True
    assert occasion_for_holiday_name("Holi (Festival of Colours)")[1] is True
    # Recognised must be False: the occasion may still default to festive, but
    # the reason shown to the user has to be phrased as a guess.
    assert occasion_for_holiday_name("Spring Bank Holiday")[1] is False
    assert occasion_for_holiday_name("August Bank Holiday")[1] is False


def test_every_holiday_mapping_names_a_real_occasion() -> None:
    """Including the default. A typo here is a 400 for every user, on a
    festival, and only on that day."""
    import yaml

    from stylist_domain.observances import CONFIG
    from stylist_domain.taxonomy import load_taxonomy

    ids = {o["id"] for o in load_taxonomy().raw["occasions"]}
    table = yaml.safe_load(CONFIG.open())
    for row in table.get("holiday_occasions") or []:
        assert row["occasion"] in ids, f"{row['match']} -> {row['occasion']}"
    assert table["holiday_default_occasion"] in ids
