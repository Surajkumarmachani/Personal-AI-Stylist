"""Phase 5: dedupe, wear log, laundry, search.

The e2e script drives a live stack; these pin the behaviour that is cheap to
assert in isolation, including the two thresholds everything else depends on.
"""

from __future__ import annotations

import io
import os
import uuid
from contextlib import contextmanager
from datetime import date, timedelta

import pytest
from httpx import AsyncClient
from PIL import Image, ImageEnhance

from stylist_domain.phash import (
    EMBEDDING_DUPLICATE_COSINE,
    PHASH_DUPLICATE_BITS,
    dhash,
    hamming,
    is_near_duplicate_hash,
)


def _garment_photo(seed: int = 0) -> Image.Image:
    img = Image.new("RGB", (400, 600), (240, 238, 235))
    px = img.load()
    assert px is not None
    for x in range(110 + seed % 40, 290 + seed % 40):
        for y in range(140, 470):
            px[x, y] = ((30 + seed * 37) % 256, (70 + seed * 11) % 256, (150 + seed * 5) % 256)
    return img


# --------------------------------------------------------------- phash


def test_dhash_is_stable_across_reencoding() -> None:
    """A re-uploaded photo must hash to (near) the same value.

    This is the whole point of the hash: the commonest duplicate is the same
    file uploaded twice, often re-encoded by the phone or the browser on the
    way.
    """
    img = _garment_photo(1)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    reencoded = Image.open(io.BytesIO(buf.getvalue()))
    assert hamming(dhash(img), dhash(reencoded)) <= PHASH_DUPLICATE_BITS


def test_dhash_survives_a_brightness_shift() -> None:
    """dHash encodes the SIGN of adjacent differences, so exposure cancels.

    An average hash would flip wholesale here, which is why this is dHash: two
    phone photos of the same garment differ in exposure almost every time.
    """
    img = _garment_photo(2)
    brighter = ImageEnhance.Brightness(img).enhance(1.4)
    assert hamming(dhash(img), dhash(brighter)) <= PHASH_DUPLICATE_BITS


def test_dhash_separates_different_garments() -> None:
    """The threshold has to EXCLUDE things, or it proposes merges constantly."""
    a, b = _garment_photo(3), _garment_photo(77)
    assert hamming(dhash(a), dhash(b)) > PHASH_DUPLICATE_BITS


def test_a_missing_hash_is_never_a_duplicate() -> None:
    """Unknown is not a match.

    A garment whose matting degraded has no cutout and so no hash. Treating
    that as a match would propose a duplicate for every unhashed garment.
    """
    h = dhash(_garment_photo(4))
    assert is_near_duplicate_hash(None, h) is False
    assert is_near_duplicate_hash(h, None) is False
    assert is_near_duplicate_hash(None, None) is False


def test_mismatched_hash_widths_raise_rather_than_guess() -> None:
    """Returning a large distance would read as 'not a duplicate' — confidently
    wrong is worse than an error the caller must handle."""
    with pytest.raises(ValueError, match="width mismatch"):
        hamming("abcd", "abcdef01")


def test_thresholds_are_where_the_plan_says() -> None:
    """Guards against a silent loosening. 0.95 is specified by the plan, and
    the bit threshold is calibrated in phash.py's docstring against measured
    distances — both are the kind of constant that gets 'tuned' in a hurry."""
    assert EMBEDDING_DUPLICATE_COSINE == 0.95
    assert PHASH_DUPLICATE_BITS == 10


# ----------------------------------------------------------- wear log


async def _a_garment(api: AsyncClient, auth: dict[str, str]) -> str | None:
    r = await api.get("/garments", headers=auth, params={"limit": 1})
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", [])
    return str(items[0]["id"]) if items else None


@pytest.mark.asyncio
async def test_wear_is_idempotent_per_day(api: AsyncClient, registered) -> None:
    """Two taps on the same day are one fact.

    Without the unique index, cost-per-wear silently halves on a double tap —
    and it is the kind of error nobody notices, because the number still looks
    plausible.
    """
    auth = registered.auth
    gid = await _a_garment(api, auth)
    if gid is None:
        pytest.fail("no garment to log a wearing against")
    first = (await api.post(f"/garments/{gid}/wear", json={}, headers=auth)).json()
    second = (await api.post(f"/garments/{gid}/wear", json={}, headers=auth)).json()
    assert first["already_logged"] is False
    assert second["already_logged"] is True
    assert second["total_wears"] == first["total_wears"]


@pytest.mark.asyncio
async def test_a_future_wearing_is_refused(api: AsyncClient, registered) -> None:
    auth = registered.auth
    gid = await _a_garment(api, auth)
    if gid is None:
        pytest.fail("no garment available")
    future = (date.today() + timedelta(days=2)).isoformat()
    resp = await api.post(f"/garments/{gid}/wear", json={"worn_on": future}, headers=auth)
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_wearing_a_garment_puts_it_in_the_basket(api: AsyncClient, registered) -> None:
    """Worn implies dirty. The reverse is not true, which is why laundry is a
    separate endpoint rather than a toggle derived from the wear log."""
    auth = registered.auth
    gid = await _a_garment(api, auth)
    if gid is None:
        pytest.fail("no garment available")
    await api.post(f"/garments/{gid}/wear", json={}, headers=auth)
    detail = (await api.get(f"/garments/{gid}/detail", headers=auth)).json()["garment"]
    assert detail["needs_wash"] is True

    await api.patch(f"/garments/{gid}/laundry", json={"needs_wash": False}, headers=auth)
    detail2 = (await api.get(f"/garments/{gid}/detail", headers=auth)).json()["garment"]
    assert detail2["needs_wash"] is False


@pytest.mark.asyncio
async def test_wear_on_another_tenants_garment_is_404(
    api: AsyncClient, registered, second_tenant
) -> None:
    """RLS makes this a 404, not a 403: we do not confirm the id exists."""
    gid = await _a_garment(api, registered.auth)
    if gid is None:
        pytest.fail("the owning tenant has no garment")
    resp = await api.post(f"/garments/{gid}/wear", json={}, headers=second_tenant.auth)
    assert resp.status_code == 404, resp.text


# ------------------------------------------------------------- search


@pytest.mark.asyncio
async def test_unknown_filter_value_is_a_400(api: AsyncClient, registered) -> None:
    """A bad enum must not reach the database and fail as a cast error, which
    surfaces to the user as a 500 on what is really a bad request."""
    resp = await api.get(
        "/garments/search", headers=registered.auth, params={"slot": "not_a_real_slot"}
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_search_query_syntax_cannot_500(api: AsyncClient, registered) -> None:
    """plainto_tsquery, not to_tsquery.

    to_tsquery is a query language: an unbalanced quote or a bare '&' raises a
    syntax error. Users type those into search boxes constantly.
    """
    for hostile in ("'", "a & ", "!!!", '"unclosed', "a | | b"):
        resp = await api.get("/garments/search", headers=registered.auth, params={"q": hostile})
        assert resp.status_code == 200, f"{hostile!r} -> {resp.status_code} {resp.text[:120]}"


@pytest.mark.asyncio
async def test_search_is_tenant_scoped(api: AsyncClient, registered, second_tenant) -> None:
    ra = (await api.get("/garments/search", headers=registered.auth)).json()
    rb = (await api.get("/garments/search", headers=second_tenant.auth)).json()
    ids_a = {i["id"] for i in ra["items"]}
    ids_b = {i["id"] for i in rb["items"]}
    assert not (ids_a & ids_b), "tenants must not see each other's garments"


@pytest.mark.asyncio
async def test_facets_never_offer_a_filter_that_matches_nothing(
    api: AsyncClient, registered
) -> None:
    """The point of facets: a taxonomy-driven filter list offers 144
    subcategories to a user with nine garments."""
    fac = (await api.get("/wardrobe/facets", headers=registered.auth)).json()
    for column in ("slot", "primary_colour", "dress_code", "material"):
        for entry in fac[column]:
            assert entry["count"] > 0, f"{column}={entry['value']} offered with no matches"


# ------------------------------------------------------- model QA provenance
#
# The eval view exists to judge the model. Its one unforgivable failure is
# presenting a tag no model produced as though it were a prediction — which is
# easy, because seeded tags, mock tags and real tags are the same columns.


@contextmanager
def _vlm_model(name: str):
    """Pin VLM_MODEL for one test.

    Settings reads the repo-root .env, so without this the assertion below is
    really about whoever last edited that file — it passed only while the
    default happened to be the mock, and broke the day a real provider was
    configured. What is under test is the LABELLING, not the ambient config.
    """
    from stylist_api.settings import get_settings

    previous = os.environ.get("VLM_MODEL")
    os.environ["VLM_MODEL"] = name
    get_settings.cache_clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("VLM_MODEL", None)
        else:
            os.environ["VLM_MODEL"] = previous
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_eval_view_labels_mock_tagging_as_not_real(api: AsyncClient, registered) -> None:
    """With the mock configured, nothing may claim to be real model output."""
    with _vlm_model("vlm-tagger-mock"):
        body = (await api.get("/garments/eval", headers=registered.auth)).json()
    for item in body["items"]:
        assert item["tag_is_real"] is False, item["tag_source"]
        assert (
            "mock" in item["tag_source"]
            or "synthetic" in item["tag_source"]
            or ("unknown" in item["tag_source"])
        ), item["tag_source"]


@pytest.mark.asyncio
async def test_nothing_tagged_yet_is_not_the_same_as_tags_are_real(
    api: AsyncClient, registered
) -> None:
    """`tagging_is_mock` is None, not False, before anything has been tagged.

    Collapsing the two would tell a brand-new user their tags are real before
    any exist — a reassurance about output that does not yet exist.
    """
    with _vlm_model("vlm-tagger"):
        body = (await api.get("/garments/eval", headers=registered.auth)).json()
    assert body["tagging_is_mock"] is None
    assert body["tagging_model_configured"] == "vlm-tagger"


@pytest.mark.asyncio
async def test_the_banner_follows_what_ran_not_what_is_configured(
    api: AsyncClient, registered, owner_engine
) -> None:
    """THE BUG THIS REPLACES.

    The label was derived from this service's own `VLM_MODEL`. But the API does
    not tag — the WORKER does — and the two drifted: the API was missing the
    variable while the worker had it, so the eval page warned that tagging was
    a deterministic stand-in while real Gemini was producing the tags. A
    warning wrong in that direction teaches people to distrust correct output.

    It now reads `model_calls`, which records what was actually called, and
    reports a disagreement rather than silently trusting either side.
    """
    import uuid as _uuid

    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        row = await session.execute(
            _text("SELECT id FROM users WHERE email = :e"), {"e": registered.email}
        )
        user_id = row.scalar_one()
        await session.execute(
            _text(
                "INSERT INTO model_calls (id, user_id, model_name, purpose) "
                "VALUES (:i, :u, 'vlm-tagger', 'tag')"
            ),
            {"i": _uuid.uuid4(), "u": user_id},
        )

    # Configured for the MOCK, but a real call is on record.
    with _vlm_model("vlm-tagger-mock"):
        body = (await api.get("/garments/eval", headers=registered.auth)).json()

    assert body["tagging_model"] == "vlm-tagger", "reports what ran"
    assert body["tagging_model_configured"] == "vlm-tagger-mock"
    assert body["tagging_is_mock"] is False, "a real call was made; no mock warning"
    assert body["tagging_config_disagrees"] is True, "and the drift is surfaced"


@pytest.mark.asyncio
async def test_another_tenants_model_calls_do_not_leak_into_the_report(
    api: AsyncClient, registered, second_tenant, owner_engine
) -> None:
    """`model_calls` has NO row-level security — verified against the schema,
    unlike every other tenant table here. So this query carries an explicit
    user_id predicate, and without it the first version reported a DIFFERENT
    tenant's model name on this tenant's page.
    """
    import uuid as _uuid

    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        other = await session.execute(
            _text("SELECT id FROM users WHERE email = :e"), {"e": second_tenant.email}
        )
        await session.execute(
            _text(
                "INSERT INTO model_calls (id, user_id, model_name, purpose) "
                "VALUES (:i, :u, 'someone-elses-model', 'tag')"
            ),
            {"i": _uuid.uuid4(), "u": other.scalar_one()},
        )

    with _vlm_model("vlm-tagger"):
        body = (await api.get("/garments/eval", headers=registered.auth)).json()

    assert body["tagging_model"] != "someone-elses-model"
    assert body["tagging_is_mock"] is None, "we have tagged nothing; their call is not ours"


@pytest.mark.asyncio
async def test_eval_view_flags_seed_script_tags_as_synthetic(
    api: AsyncClient, registered, owner_engine
) -> None:
    """A garment tagged by scripts/seed_demo.py must say so.

    Judging accuracy on generated tags measures the generator, and the seeded
    rows outnumber the real ones by an order of magnitude in this database.
    """
    from sqlalchemy import text as sql

    async with owner_engine.begin() as conn:
        uid = (
            await conn.execute(
                sql("SELECT id FROM users WHERE email = :e"), {"e": registered.email}
            )
        ).scalar_one()
        gid = uuid.uuid4()
        await conn.execute(
            sql(
                """
                INSERT INTO garments (id, user_id, original_key, state, extractor_version,
                                      is_active, created_at, updated_at)
                VALUES (:id, :uid, :key, 'matted', 'seed-demo-v1', true, now(), now())
                """
            ),
            {"id": gid, "uid": uid, "key": f"originals/{uid}/{gid}"},
        )
    body = (await api.get("/garments/eval", headers=registered.auth)).json()
    seeded = [i for i in body["items"] if i["id"] == str(gid)]
    assert seeded, "the seeded garment is missing from the eval view"
    assert "synthetic" in seeded[0]["tag_source"], seeded[0]["tag_source"]
    assert seeded[0]["tag_is_real"] is False


@pytest.mark.asyncio
async def test_eval_view_only_real_excludes_synthetic(
    api: AsyncClient, registered, owner_engine
) -> None:
    from sqlalchemy import text as sql

    async with owner_engine.begin() as conn:
        uid = (
            await conn.execute(
                sql("SELECT id FROM users WHERE email = :e"), {"e": registered.email}
            )
        ).scalar_one()
        gid = uuid.uuid4()
        await conn.execute(
            sql(
                """
                INSERT INTO garments (id, user_id, original_key, state, extractor_version,
                                      is_active, created_at, updated_at)
                VALUES (:id, :uid, :key, 'matted', 'seed-demo-v1', true, now(), now())
                """
            ),
            {"id": gid, "uid": uid, "key": f"originals/{uid}/{gid}"},
        )
    filtered = (
        await api.get("/garments/eval", headers=registered.auth, params={"only_real": "true"})
    ).json()
    assert not any(i["id"] == str(gid) for i in filtered["items"])


@pytest.mark.asyncio
async def test_eval_view_marks_user_verified_fields(api: AsyncClient, registered) -> None:
    """A corrected field is ground truth and must not count as a prediction.

    Without this, every correction a user makes inflates the model's apparent
    accuracy — the metric improves precisely because the model was wrong.
    """
    gid = await _a_garment(api, registered.auth)
    if gid is None:
        pytest.fail("no garment available")
    resp = await api.patch(
        f"/garments/{gid}/fields",
        json={"field_name": "material", "new_value": "cotton"},
        headers=registered.auth,
    )
    assert resp.status_code == 200, resp.text
    body = (await api.get("/garments/eval", headers=registered.auth)).json()
    item = next(i for i in body["items"] if i["id"] == gid)
    material = next(f for f in item["fields"] if f["field"] == "material")
    assert material["user_verified"] is True, material


@pytest.mark.asyncio
async def test_eval_view_is_tenant_scoped(api: AsyncClient, registered, second_tenant) -> None:
    a = (await api.get("/garments/eval", headers=registered.auth)).json()
    b = (await api.get("/garments/eval", headers=second_tenant.auth)).json()
    assert not ({i["id"] for i in a["items"]} & {i["id"] for i in b["items"]})


# ------------------------------------------------- read-your-own-writes
#
# THESE TESTS CANNOT CATCH THE RACE THEY DESCRIBE. Stated plainly because a
# test that looks like a guard and is not is worse than no test.
#
# The bug is that FastAPI commits a `yield` dependency AFTER the response is
# sent, so a client re-reading immediately can observe pre-write state. pytest
# drives the app in-process through httpx's ASGITransport, which awaits the
# entire response cycle INCLUDING dependency teardown before returning — so
# the commit has always happened by the time the next call runs. Verified by
# mutation: reverting `unlog_wear` to the dependency-owned transaction leaves
# these tests passing.
#
# What they do cover: that the endpoints work at all, and the expected values.
# What actually caught the bug, and what guards it, is
# scripts/verify_phase5_e2e.py, which drives the API over a real socket from a
# separate process — where it failed roughly 1 run in 4 and now passes 8 of 8.
# That script is in the gate suite for this reason.
#
# No sleeps here regardless: a wait would also mask a genuine ordering bug if
# one were ever reachable in-process.


@pytest.mark.asyncio
async def test_a_logged_wear_is_visible_immediately(api: AsyncClient, registered) -> None:
    auth = registered.auth
    gid = await _a_garment(api, auth)
    if gid is None:
        pytest.fail("no garment available")
    await api.post(f"/garments/{gid}/wear", json={}, headers=auth)
    # Immediately, with no wait.
    history = (await api.get(f"/garments/{gid}/wears", headers=auth)).json()
    assert history["total_wears"] == 1, history


@pytest.mark.asyncio
async def test_an_unlogged_wear_is_gone_immediately(api: AsyncClient, registered) -> None:
    """The exact failure that showed up as a 1-in-4 flake.

    DELETE returned 204 while the transaction was still uncommitted, so the
    immediate re-read could still count the deleted wearing.
    """
    auth = registered.auth
    gid = await _a_garment(api, auth)
    if gid is None:
        pytest.fail("no garment available")
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    await api.post(f"/garments/{gid}/wear", json={}, headers=auth)
    await api.post(f"/garments/{gid}/wear", json={"worn_on": yesterday}, headers=auth)
    assert (await api.get(f"/garments/{gid}/wears", headers=auth)).json()["total_wears"] == 2

    await api.request("DELETE", f"/garments/{gid}/wear/{yesterday}", headers=auth)
    after = (await api.get(f"/garments/{gid}/wears", headers=auth)).json()
    assert after["total_wears"] == 1, after


@pytest.mark.asyncio
async def test_a_laundry_change_is_visible_immediately(api: AsyncClient, registered) -> None:
    auth = registered.auth
    gid = await _a_garment(api, auth)
    if gid is None:
        pytest.fail("no garment available")
    await api.patch(f"/garments/{gid}/laundry", json={"needs_wash": True}, headers=auth)
    detail = (await api.get(f"/garments/{gid}/detail", headers=auth)).json()["garment"]
    assert detail["needs_wash"] is True


@pytest.mark.asyncio
async def test_a_correction_is_visible_immediately(api: AsyncClient, registered) -> None:
    """Phase 4's handler had the same latent bug.

    The correction UI re-reads the garment as soon as it gets 200, so an
    uncommitted UPDATE renders the value the user just replaced — which reads
    as "my correction didn't save".
    """
    auth = registered.auth
    gid = await _a_garment(api, auth)
    if gid is None:
        pytest.fail("no garment available")
    resp = await api.patch(
        f"/garments/{gid}/fields",
        json={"field_name": "material", "new_value": "linen"},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    detail = (await api.get(f"/garments/{gid}/detail", headers=auth)).json()["garment"]
    assert detail["material"] == "linen", detail
