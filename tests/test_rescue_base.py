"""A base outfit is rescued rather than lost to one filter.

The bug, on a real account: four pairs of warmth-3 denim jeans, a forecast of
30°C and rain. The heat ruled out warmth 3 (target 1) and the rain ruled out
denim, the rescue only covered `feet`, and every occasion answered "add
something to wear on the bottom" to someone who owns four pairs.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from stylist_db.session import tenant_session
from stylist_domain.context import resolve_context
from stylist_suggest import load_wardrobe, suggest

pytestmark = pytest.mark.asyncio


async def _garment(db, user_id, **kw) -> None:
    cols = {
        "slot": "upper_base",
        "subcategory": "shirt_casual",
        "primary_colour": "white",
        "material": "cotton",
        "fit": "regular",
        "formality": 2,
        "warmth": 2,
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
        {"g": uuid.uuid4(), "u": user_id, **cols},
    )


@pytest.fixture
async def denim_only(owner_engine, initialised_engine):
    """Cotton shirts, and bottoms that are ONLY warmth-3 denim jeans."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    user_id = uuid.uuid4()
    async with maker() as session, session.begin():
        await session.execute(
            text("INSERT INTO users (id, email, password_hash) VALUES (:id, :e, 'x')"),
            {"id": user_id, "e": f"rescue-{user_id}@example.com"},
        )
    async with tenant_session(user_id) as db:
        for colour in ("white", "black"):
            await _garment(db, user_id, primary_colour=colour)
        for colour in ("blue_light", "blue_navy"):
            await _garment(
                db,
                user_id,
                slot="lower",
                subcategory="jeans",
                primary_colour=colour,
                material="denim",
                warmth=3,
            )
    yield user_id
    async with maker() as session, session.begin():
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})


async def test_a_heatwave_does_not_erase_every_bottom(denim_only) -> None:
    ctx = resolve_context(occasion="casual_outing", feels_like_c=31.0)
    assert ctx.warmth_target == 1, "the premise: warmth 3 is out of tolerance"
    async with tenant_session(denim_only) as db:
        pool = await load_wardrobe(db, ctx)
    assert len(pool.by_slot.get("lower", [])) == 2
    assert any("warmth match for lower" in n for n in pool.notes)
    assert suggest(pool, ctx, limit=4).outfits, "jeans + shirt is an outfit"


async def test_rain_on_top_of_heat_still_leaves_an_outfit_and_says_why(denim_only) -> None:
    ctx = resolve_context(occasion="casual_outing", feels_like_c=31.0, precip_probability=0.95)
    assert ctx.wet, "the premise: denim is `poor` when wet"
    async with tenant_session(denim_only) as db:
        pool = await load_wardrobe(db, ctx)
    assert len(pool.by_slot.get("lower", [])) == 2
    assert any("rainy" in n and "denim" in n for n in pool.notes), pool.notes
    assert suggest(pool, ctx, limit=4).outfits


async def test_nothing_is_relaxed_when_the_filters_already_leave_an_outfit(denim_only) -> None:
    ctx = resolve_context(occasion="casual_outing", feels_like_c=24.0)
    async with tenant_session(denim_only) as db:
        pool = await load_wardrobe(db, ctx)
    assert pool.by_slot.get("lower") and pool.by_slot.get("upper_base")
    assert not any("relaxed" in n or "rainy" in n for n in pool.notes), pool.notes
