"""Phase 8 — preference facts, and whether the pipeline actually obeys them.

WHY THESE TESTS EXIST BEFORE THE UI DOES
-----------------------------------------
The facts had an API and a table for a while, and NOTHING read them. A screen
showing "never: yellow" while yellow keeps being suggested is worse than no
screen: it is a visible promise nothing keeps, and the plan's whole argument
for this feature is that "legibility buys trust faster than accuracy does".
Legibility only buys trust while what is shown is also what is enforced.

So the assertions here are about the CANDIDATE POOL, not about the endpoint
that stores a fact. Storing one was never the hard part.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from stylist_db.session import tenant_session
from stylist_domain.context import resolve_context
from stylist_suggest import load_wardrobe

CTX = resolve_context(occasion="casual_outing", feels_like_c=26.0)


async def _garment(db, user_id, **kw) -> uuid.UUID:
    gid = uuid.uuid4()
    cols = {
        "slot": "upper_base",
        "subcategory": "t_shirt",
        "primary_colour": "maroon",
        "material": "cotton",
        "fit": "regular",
        "formality": 3,
        "warmth": 3,
        "dress_code": "casual",
        **kw,
    }
    await db.execute(
        text(
            "INSERT INTO garments (id, user_id, original_key, state, slot, subcategory, "
            "primary_colour, material, fit, formality, warmth, dress_code) VALUES "
            "(:g, :u, 'k', 'complete', CAST(:slot AS slot), CAST(:subcategory AS subcategory), "
            "CAST(:primary_colour AS colour), CAST(:material AS material), "
            "CAST(:fit AS fit), :formality, :warmth, CAST(:dress_code AS dress_code))"
        ),
        {"g": gid, "u": user_id, **cols},
    )
    return gid


async def _fact(db, user_id, kind: str, field: str, value: str) -> None:
    await db.execute(
        text(
            "INSERT INTO preference_fact (id, user_id, kind, field_name, field_value, source) "
            "VALUES (:i, :u, CAST(:k AS preference_fact_kind), :f, :v, 'user')"
        ),
        {"i": uuid.uuid4(), "u": user_id, "k": kind, "f": field, "v": value},
    )


@pytest.fixture
async def wardrobe(owner_engine, initialised_engine):
    """A tenant with a small, deliberately monotonous wardrobe."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    user_id = uuid.uuid4()
    async with maker() as session, session.begin():
        await session.execute(
            text("INSERT INTO users (id, email, password_hash) VALUES (:id, :e, 'x')"),
            {"id": user_id, "e": f"pf-{user_id}@example.com"},
        )
    yield user_id
    async with maker() as session, session.begin():
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})


# ------------------------------------------------------------ never


async def test_never_is_a_hard_exclusion(wardrobe) -> None:
    """ "Never yellow" is not "yellow ranks lower". The word means what it says,
    so it is enforced in SQL and the garment does not enter the pool at all."""
    async with tenant_session(wardrobe) as db:
        keep = await _garment(db, wardrobe, primary_colour="maroon")
        drop = await _garment(db, wardrobe, primary_colour="yellow")
        await _fact(db, wardrobe, "never", "primary_colour", "yellow")

        pool = await load_wardrobe(db, CTX)

    ids = {g.garment_id for items in pool.by_slot.values() for g in items}
    assert str(keep) in ids
    assert str(drop) not in ids, "a 'never' garment must not be a candidate"


async def test_never_applies_to_every_supported_field(wardrobe) -> None:
    """A fact the pipeline cannot apply is a promise nothing keeps, so every
    field the API accepts has to be enforced here."""
    async with tenant_session(wardrobe) as db:
        await _garment(db, wardrobe, subcategory="crop_top")
        await _garment(db, wardrobe, material="leather")
        await _garment(db, wardrobe, fit="oversized")
        keep = await _garment(db, wardrobe, subcategory="t_shirt")

        await _fact(db, wardrobe, "never", "subcategory", "crop_top")
        await _fact(db, wardrobe, "never", "material", "leather")
        await _fact(db, wardrobe, "never", "fit", "oversized")

        pool = await load_wardrobe(db, CTX)

    ids = {g.garment_id for items in pool.by_slot.values() for g in items}
    assert ids == {str(keep)}


async def test_another_tenants_facts_do_not_filter_your_wardrobe(wardrobe, owner_engine) -> None:
    """`preference_fact` is RLS-scoped, so the subquery needs no user_id
    predicate. Asserted rather than assumed: a missing predicate that RLS
    happens to cover is one policy change away from filtering everyone's
    wardrobe by one person's rules."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    other = uuid.uuid4()
    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        await session.execute(
            text("INSERT INTO users (id, email, password_hash) VALUES (:id, :e, 'x')"),
            {"id": other, "e": f"pf-{other}@example.com"},
        )
    try:
        async with tenant_session(other) as db:
            await _fact(db, other, "never", "primary_colour", "maroon")

        async with tenant_session(wardrobe) as db:
            mine = await _garment(db, wardrobe, primary_colour="maroon")
            pool = await load_wardrobe(db, CTX)

        ids = {g.garment_id for items in pool.by_slot.values() for g in items}
        assert str(mine) in ids, "someone else's 'never' must not touch my wardrobe"
    finally:
        async with maker() as session, session.begin():
            await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": other})


# ------------------------------------------------------------ avoids


async def test_avoids_hides_garments_when_there_is_an_alternative(wardrobe) -> None:
    async with tenant_session(wardrobe) as db:
        keep = await _garment(db, wardrobe, subcategory="t_shirt")
        avoid = await _garment(db, wardrobe, subcategory="crop_top")
        await _fact(db, wardrobe, "avoids", "subcategory", "crop_top")

        pool = await load_wardrobe(db, CTX)

    ids = {g.garment_id for items in pool.by_slot.values() for g in items}
    assert str(keep) in ids and str(avoid) not in ids
    assert any("avoids" in n for n in pool.notes), "the UI needs to be able to explain this"


async def test_avoids_is_relaxed_rather_than_emptying_a_slot(wardrobe) -> None:
    """ "I avoid crop tops" means "not usually", not "I would rather have no
    outfit". A wardrobe where every top is avoided should still produce one —
    and SAY that the preference was overridden, because a silently ignored
    rule is how a legible system stops being trusted."""
    async with tenant_session(wardrobe) as db:
        only = await _garment(db, wardrobe, subcategory="crop_top")
        await _fact(db, wardrobe, "avoids", "subcategory", "crop_top")

        pool = await load_wardrobe(db, CTX)

    ids = {g.garment_id for items in pool.by_slot.values() for g in items}
    assert str(only) in ids, "relaxed rather than leaving the slot empty"
    assert any("relaxed" in n for n in pool.notes)


async def test_avoids_and_never_differ_on_exactly_this_point(wardrobe) -> None:
    """The one behavioural difference between the two kinds, asserted directly:
    `never` empties the slot, `avoids` does not."""
    async with tenant_session(wardrobe) as db:
        await _garment(db, wardrobe, subcategory="crop_top")
        await _fact(db, wardrobe, "never", "subcategory", "crop_top")

        pool = await load_wardrobe(db, CTX)

    ids = {g.garment_id for items in pool.by_slot.values() for g in items}
    assert ids == set(), "'never' is absolute even when it leaves nothing"


async def test_no_facts_means_no_filtering_and_no_notes(wardrobe) -> None:
    """The counterweight. A filter that fires when there is nothing to filter
    would quietly shrink every wardrobe in the product."""
    async with tenant_session(wardrobe) as db:
        a = await _garment(db, wardrobe, subcategory="t_shirt")
        b = await _garment(db, wardrobe, subcategory="crop_top")
        pool = await load_wardrobe(db, CTX)

    ids = {g.garment_id for items in pool.by_slot.values() for g in items}
    assert ids == {str(a), str(b)}
    assert not any("avoids" in n for n in pool.notes)


# ------------------------------- the materialised path, which forgot the rules


async def test_the_materialised_path_obeys_never_rules(wardrobe) -> None:
    """THE BUG THIS SECTION EXISTS FOR.

    `load_wardrobe` applies preference facts, but it only runs when an outfit
    is generated LIVE. A materialised outfit was precomputed last night, so the
    read path ignored the rules entirely — measured against the running stack
    as ten white garments served with an active `never white`, i.e. up to 24
    hours of the UI showing a rule the product was not applying.

    The earlier check that "looked" fine was a FALSE POSITIVE: the top-10
    happened to contain no garment of the banned colour, so the test proved
    nothing. Hence this one picks the colour out of what is actually served.
    """
    from stylist_api.routers.suggestions import _hydrate

    class FakeStore:
        def presign_download(self, key: str) -> str:
            return f"https://example/{key}"

    async with tenant_session(wardrobe) as db:
        keep = await _garment(db, wardrobe, primary_colour="maroon")
        banned = await _garment(db, wardrobe, primary_colour="yellow")

        rows = [
            {"garment_ids": [keep], "score": 0.9, "score_breakdown": {}},
            {"garment_ids": [banned], "score": 0.8, "score_breakdown": {}},
        ]

        before, _ = await _hydrate(db, FakeStore(), rows)
        assert len(before) == 2, "both outfits render with no rule set"

        await _fact(db, wardrobe, "never", "primary_colour", "yellow")
        after, _ = await _hydrate(db, FakeStore(), rows)

    assert len(after) == 1, "the outfit containing a 'never' garment is dropped"
    assert after[0]["_ids"] == [str(keep)]


async def test_a_never_rule_does_not_produce_a_blank_screen(wardrobe) -> None:
    """Filtering can empty the whole materialised set — measured: `never white`
    dropped all ten precomputed outfits. The endpoint then regenerates live,
    which honours the rules at generation time, so the user gets suggestions
    that obey the rule rather than a blank screen for having set one.

    Asserted at the pool level: live generation must still find candidates
    after the SQL exclusion, which is what makes that fallback worth having.
    """
    async with tenant_session(wardrobe) as db:
        await _garment(db, wardrobe, primary_colour="yellow")
        await _garment(db, wardrobe, primary_colour="maroon", slot="lower", subcategory="jeans")
        await _garment(db, wardrobe, primary_colour="maroon", slot="feet", subcategory="sneakers")
        await _fact(db, wardrobe, "never", "primary_colour", "yellow")

        pool = await load_wardrobe(db, CTX)

    colours = {g.primary_colour for items in pool.by_slot.values() for g in items}
    assert "yellow" not in colours
    assert pool.total > 0, "live generation still has something to work with"
