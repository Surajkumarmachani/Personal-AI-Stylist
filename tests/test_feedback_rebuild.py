"""Phase 8 exit criterion: the rebuild reproduces live style vectors.

`tests/test_style.py` proves the FOLD is replayable — same function, same
order, same answer. This file proves the two real callers agree: the HTTP
handler folding events as they arrive, and `scripts/rebuild_style_vectors.py`
replaying the log from scratch through Postgres.

That is a different claim. The live vector round-trips through pgvector (float4)
once per event while the replay holds float64 throughout, the embeddings come
from real rows rather than fixtures, and the tenant enumeration goes through a
SECURITY DEFINER function that RLS could silently empty. None of that is
exercised by a pure unit test, and every one of it has broken something in this
project before.
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from scripts.rebuild_style_vectors import TOLERANCE, max_abs_diff, replay_tenant, stored_vector
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

DIM = 768


def unit(direction: int) -> list[float]:
    """A distinct unit vector per direction, so "moved toward" is unambiguous."""
    vec = [0.0] * DIM
    vec[direction] = 1.0
    return vec


@pytest_asyncio.fixture
async def embedded(api, registered, owner_engine):
    """An authenticated tenant whose wardrobe has real embeddings.

    Built on `registered` because the feedback path goes through HTTP and needs
    a real token, but that fixture's single garment sits at `received` with a
    NULL embedding — nothing for a style vector to average. The four garments
    added here carry orthogonal unit vectors so "moved toward" is unambiguous
    rather than a matter of floating-point luck.
    """
    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with maker() as session:
        row = await session.execute(
            text("SELECT id FROM users WHERE email = :e"), {"e": registered.email}
        )
        user_id = row.scalar_one()

    garment_ids: list[uuid.UUID] = []
    async with maker() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(user_id)}
        )
        for i, slot in enumerate(("upper_base", "lower", "feet", "upper_layer")):
            gid = uuid.uuid4()
            garment_ids.append(gid)
            await session.execute(
                text(
                    "INSERT INTO garments "
                    "(id, user_id, original_key, slot, primary_colour, state, embedding) "
                    "VALUES (:g, :uid, :k, CAST(:slot AS slot), 'maroon', 'complete', :emb)"
                ),
                {
                    "g": gid,
                    "uid": user_id,
                    "k": f"originals/{user_id}/emb{i}",
                    "slot": slot,
                    "emb": str(unit(i)),
                },
            )

    class _E:
        pass

    e = _E()
    e.user_id = user_id
    e.token = registered.token
    e.auth = registered.auth
    e.garment_ids = garment_ids
    e.client = api
    return e


async def _post(client, token: str, ids: list[uuid.UUID], kind: str, **kw):
    return await client.post(
        "/outfits/feedback",
        json={"garment_ids": [str(g) for g in ids], "kind": kind, **kw},
        headers={"Authorization": f"Bearer {token}"},
    )


# ------------------------------------------------ THE exit criterion


async def test_the_replay_reproduces_the_live_vector(embedded) -> None:
    """PHASE 8 EXIT CRITERION, through the real stack.

    Five events over the HTTP handler, then the same log replayed by the
    script. The vectors must agree — and agreeing here means the live fold, the
    pgvector round trip, the replay ORDER and the embedding fetch all line up,
    none of which `test_style.py` can see.
    """
    client, token = embedded.client, embedded.token
    user_id, garment_ids = embedded.user_id, embedded.garment_ids

    kinds = ["like", "worn", "dislike", "saved", "worn"]
    for i, kind in enumerate(kinds):
        outfit = [garment_ids[i % len(garment_ids)], garment_ids[(i + 1) % len(garment_ids)]]
        resp = await _post(client, token, outfit, kind)
        assert resp.status_code == 201, resp.text

    live = await stored_vector(user_id)
    assert live is not None, "the handler must have written a vector"
    assert live.events_applied == len(kinds)

    replayed, event_rows = await replay_tenant(user_id)
    assert replayed is not None
    assert event_rows == len(kinds), "every logged event was read back"
    assert replayed.events_applied == live.events_applied
    assert replayed.last_event_id == live.last_event_id

    delta = max_abs_diff(replayed.vector, live.vector)
    assert delta <= TOLERANCE, f"replay diverged from live by {delta:.2e}"


async def test_a_divergent_stored_vector_is_detected(embedded) -> None:
    """The criterion is only worth asserting if it can FAIL.

    Corrupt the stored vector and confirm the comparison notices. Without this,
    a rebuild that always reported "consistent" would pass the exit criterion
    while proving nothing — the same class of green-but-blind check this
    project has hit four times.
    """
    client, token = embedded.client, embedded.token
    user_id, garment_ids = embedded.user_id, embedded.garment_ids
    await _post(client, token, garment_ids[:2], "like")

    from stylist_db.session import tenant_session

    async with tenant_session(user_id) as db:
        await db.execute(
            text("UPDATE user_style_vector SET vector = :v"),
            {"v": str([0.0] * (DIM - 1) + [1.0])},
        )

    stored = await stored_vector(user_id)
    replayed, _ = await replay_tenant(user_id)
    assert stored is not None and replayed is not None
    assert max_abs_diff(replayed.vector, stored.vector) > TOLERANCE


# ------------------------------------------------------ the log itself


async def test_feedback_is_recorded_even_when_it_moves_nothing(embedded) -> None:
    """`dismissed` carries no taste signal, but the ROW still exists.

    The log is what every derived thing replays. An event the handler declined
    to record because it would not move the vector is an event no future
    scoring change can ever learn from — and dismissals are exactly what a
    later "why did they skip this" analysis would want.
    """
    client, token = embedded.client, embedded.token
    user_id, garment_ids = embedded.user_id, embedded.garment_ids
    resp = await _post(client, token, garment_ids[:2], "dismissed")
    assert resp.status_code == 201

    from stylist_db.session import tenant_session

    async with tenant_session(user_id) as db:
        rows = await db.execute(
            text("SELECT kind::text AS kind FROM outfit_feedback WHERE kind = 'dismissed'")
        )
        assert len(list(rows)) == 1


async def test_feedback_on_someone_elses_garment_is_a_404(
    embedded, second_tenant, owner_engine
) -> None:
    """RLS scopes the lookup, so another tenant's garment simply is not found.
    Reported as 404 rather than 403: confirming the id exists would leak that
    somebody owns it."""
    client, token = embedded.client, embedded.token

    # The other tenant's real, live garment id — read as the owner so the test
    # is about the API's isolation rather than about our own ability to query.
    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with maker() as session:
        row = await session.execute(
            text(
                "SELECT g.id FROM garments g JOIN users u ON u.id = g.user_id "
                "WHERE u.email = :e LIMIT 1"
            ),
            {"e": second_tenant.email},
        )
        other_garment = row.scalar_one()

    resp = await _post(client, token, [other_garment], "like")
    assert resp.status_code == 404


async def test_wear_through_is_none_not_zero_when_nothing_was_suggested(embedded) -> None:
    """A rate over no suggestions is UNMEASURED, not 0%. The difference decides
    whether the ranking is bad or simply untested — and Phase 8 calls this the
    only quality metric that matters, so reporting a confident 0 would be the
    worst possible default."""
    client, token = embedded.client, embedded.token
    resp = await client.get("/me/wear-through", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["wear_through_rate"] is None


async def test_wear_through_counts_distinct_outfits_not_events(embedded) -> None:
    """Three taps on one card is still one suggestion. Counting events would
    let indecision inflate the rate."""
    client, token = embedded.client, embedded.token
    garment_ids = embedded.garment_ids
    outfit = garment_ids[:2]
    for kind in ("like", "dislike", "worn"):
        assert (await _post(client, token, outfit, kind)).status_code == 201

    resp = await client.get("/me/wear-through", headers={"Authorization": f"Bearer {token}"})
    body = resp.json()
    assert body["suggested_outfits"] == 1
    assert body["worn_outfits"] == 1
    assert body["wear_through_rate"] == 1.0
