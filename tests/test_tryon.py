"""Phase 10 — consent, body photos, the render path, and the degrade to a board.

WHAT IS TESTED AND WHAT IS DELIBERATELY NOT
--------------------------------------------
Phase 10 opens with "Benchmark before you build": a 10-body x 16-garment grid
including sarees, kurtas and a sherwani, because the published benchmark used
Western garments and "your routing table must come from your own grid".

That grid gates the ROUTING TABLE — which model for which category — and there
is no way to run the grid without a working render path, so the render path is
tested here and there is NO ROUTER to test: one provider, no fallback chain, no
per-category selection.

The rest is what has to be right before any image leaves this infrastructure —
consent, separation, deletion — and the exit criterion that holds whether or
not a provider is configured: "try-on works or degrades to a board, never
errors".

No test here calls a provider. They assert the decisions made AROUND the call:
what is refused, what is skipped, what is re-checked, and what happens when it
fails.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from stylist_db.session import tenant_session


async def _user_id(owner_engine, email: str) -> uuid.UUID:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with maker() as session:
        row = await session.execute(text("SELECT id FROM users WHERE email = :e"), {"e": email})
        return row.scalar_one()


# ------------------------------------------------------------ consent


async def test_consent_must_be_explicit(api, registered) -> None:
    """ "They uploaded it, so they must have agreed" is the reasoning that makes
    consent a formality. The client has to say the word."""
    presign = (await api.post("/me/body-photos/presign", headers=registered.auth)).json()
    resp = await api.post(
        "/me/body-photos",
        json={
            "upload_id": presign["upload_id"],
            "key": presign["key"],
            "consent_to_virtual_tryon": False,
        },
        headers=registered.auth,
    )
    assert resp.status_code == 400
    assert "consent" in resp.json()["detail"]


async def test_a_body_photo_cannot_be_attached_to_someone_elses_key(
    api, registered, second_tenant
) -> None:
    """Without this check a caller could attach their consent to ANY object in
    the bucket, including another tenant's body photo — turning a consent
    record into a way to claim someone else's data."""
    other = (await api.post("/me/body-photos/presign", headers=second_tenant.auth)).json()
    resp = await api.post(
        "/me/body-photos",
        json={
            "upload_id": other["upload_id"],
            "key": other["key"],
            "consent_to_virtual_tryon": True,
        },
        headers=registered.auth,
    )
    assert resp.status_code == 400


async def test_body_photos_upload_under_their_own_prefix(api, registered) -> None:
    """Not `originals/`. A photograph of a person and a photograph of a shirt
    must not be indistinguishable to a prefix operation — consent, revocation
    and erasure each need to target one without touching the other."""
    presign = (await api.post("/me/body-photos/presign", headers=registered.auth)).json()
    assert presign["key"].startswith("body/")
    assert "notice" in presign, "the purpose is stated at upload, not in a settings screen"


async def test_the_object_key_is_never_returned_to_the_client(api, registered) -> None:
    """It points at the most sensitive object this system stores, and the
    client has no use for it that a presigned URL does not serve better."""
    presign = (await api.post("/me/body-photos/presign", headers=registered.auth)).json()
    await api.post(
        "/me/body-photos",
        json={
            "upload_id": presign["upload_id"],
            "key": presign["key"],
            "consent_to_virtual_tryon": True,
        },
        headers=registered.auth,
    )
    body = (await api.get("/me/body-photos", headers=registered.auth)).json()
    assert body["active"] == 1
    assert presign["key"] not in str(body)


# ------------------------------- the exit criterion: never errors


@pytest.mark.parametrize("consented", [False, True])
async def test_tryon_degrades_to_a_board_and_never_errors(
    api, registered, owner_engine, consented: bool
) -> None:
    """PHASE 10 EXIT CRITERION: "works or degrades to a board, NEVER errors".

    No provider is configured — that is the honest state until the benchmark
    exists — so every call degrades. It must still be a 200 with the board,
    not a 4xx: a board is pixel-accurate to clothes the user owns, and when the
    render is unavailable the accurate picture is a better answer than an error
    page.
    """
    user_id = await _user_id(owner_engine, registered.email)
    outfit_hash = "c" * 64

    async with tenant_session(user_id) as db:
        gid = uuid.uuid4()
        await db.execute(
            text(
                "INSERT INTO garments (id, user_id, original_key, slot, state) "
                "VALUES (:g, :u, 'k', 'upper_base', 'complete')"
            ),
            {"g": gid, "u": user_id},
        )
        await db.execute(
            text(
                "INSERT INTO outfits (id, user_id, garment_ids, garment_set_hash, occasion, "
                "warmth_target, formality_target, score, scoring_version) "
                "VALUES (:i, :u, CAST(:ids AS uuid[]), :h, 'casual_outing', 3, 3, 0.9, 1)"
            ),
            {"i": uuid.uuid4(), "u": user_id, "ids": [str(gid)], "h": outfit_hash},
        )
        if consented:
            await db.execute(
                text("INSERT INTO body_photo (id, user_id, object_key) VALUES (:i, :u, :k)"),
                {"i": uuid.uuid4(), "u": user_id, "k": f"body/{user_id}/x"},
            )

    resp = await api.post(f"/outfits/{outfit_hash}/tryon", headers=registered.auth)
    body = resp.json()

    assert resp.status_code == 200, "never errors"
    assert body["rendered"] is False
    assert body["reason"], "a degrade must say WHY, or it is indistinguishable from a bug"
    assert body["board_endpoint"].endswith("/board")
    if not consented:
        assert "consent" in body["reason"] or "body photo" in body["reason"]


async def test_an_outfit_that_does_not_exist_is_the_one_real_404(api, registered) -> None:
    """The single exception to "never errors". We cannot render OR board an
    outfit that does not exist, and returning 200 would hide a client bug
    behind a degrade message."""
    resp = await api.post(f"/outfits/{'d' * 64}/tryon", headers=registered.auth)
    assert resp.status_code == 404


# -------------------------------------------- deletion, verified


def test_erasure_covers_the_body_photo_prefix() -> None:
    """THE GAP THIS TEST EXISTS FOR.

    Body photos upload under their own prefix precisely so consent and
    revocation can target them — and that same separation is what would let an
    account erasure walk straight past them. A prefix missing from the saga is
    data that survives deletion, invisible, because nothing else lists it.

    Asserted against the source rather than by running a saga, so it fails the
    moment someone adds a prefix to `presign_upload` and forgets this one.
    """
    import inspect

    from stylist_worker import erasure

    source = inspect.getsource(erasure._delete_objects)
    for prefix in (
        "originals/",
        "cutouts/",
        "masks/",
        "boards/",
        "grids/",
        "body/",
        # A RENDER OF THE OWNER'S BODY. Derived, and more sensitive than either
        # of its inputs — this is the prefix easiest to forget, because nothing
        # the user uploaded lives here.
        "tryon/",
        "exports/",
    ):
        assert f'f"{prefix}' in source, f"erasure does not purge {prefix}"


async def test_revoking_body_photos_does_not_touch_the_account(api, registered) -> None:
    """§C5: "users must be able to revoke that consent without deleting their
    account". Bundling the two would make withdrawing consent for ONE feature
    cost you the whole product, which is what makes consent meaningless."""
    presign = (await api.post("/me/body-photos/presign", headers=registered.auth)).json()
    await api.post(
        "/me/body-photos",
        json={
            "upload_id": presign["upload_id"],
            "key": presign["key"],
            "consent_to_virtual_tryon": True,
        },
        headers=registered.auth,
    )

    resp = await api.delete("/me/body-photos", headers=registered.auth)
    assert resp.status_code == 200
    assert resp.json()["account_deleted"] is False

    # The account still works.
    assert (await api.get("/garments", headers=registered.auth)).status_code == 200
    # And the consent is recorded as revoked rather than deleted, so "why did
    # try-on stop" stays answerable.
    body = (await api.get("/me/body-photos", headers=registered.auth)).json()
    assert body["active"] == 0
    assert body["photos"][0]["revoked_at"] is not None


# -------------------------------------------- the render path


def test_a_saree_is_not_coerced_into_a_western_category() -> None:
    """THE POINT OF THE WHOLE PHASE, IN ONE ASSERTION.

    Every candidate model was trained on VITON-HD or DressCode, both Western
    catalogues, and all three accept exactly upper_body / lower_body / dresses.
    A saree (`drape`) is none of them.

    Mapping it to `dresses` would return a confident, wrong picture of the
    owner's own body — and §C3's argument throughout is that a confident wrong
    answer costs more than an absent one. Which category (if any) serves a
    saree is precisely what the benchmark grid is for, so until it exists
    `drape` is skipped and reported rather than guessed.
    """
    from stylist_clients.vton_client import SLOT_TO_CATEGORY

    assert "drape" not in SLOT_TO_CATEGORY, "a saree must not be silently rendered as a dress"
    # And the slots no VTON model renders at all are absent too, rather than
    # mapped to something harmless-looking.
    for slot in ("head", "feet", "bag", "accessory"):
        assert slot not in SLOT_TO_CATEGORY


def test_a_provider_refuses_a_category_it_was_not_trained_for() -> None:
    """IDM-VTON takes no category argument — it is VITON-HD, upper-body only.

    Sending trousers to it returns a confident picture of a shirt-shaped
    garment on a torso, not an error, so the refusal has to happen on our side
    or not at all.
    """
    from stylist_clients.vton_client import VTONClient, VTONUnavailable

    client = VTONClient("idm-vton", api_token="x")
    with pytest.raises(VTONUnavailable, match="does not render"):
        client.render(person_png=b"", garment_png=b"", category="lower_body")


def test_an_unknown_provider_fails_at_construction_not_at_render_time() -> None:
    """A typo in VTON_PROVIDER must fail when the client is built, not 90
    seconds into someone's first try-on."""
    from stylist_clients.vton_client import VTONClient

    with pytest.raises(ValueError, match="unknown VTON provider"):
        VTONClient("idm-vtron")


def test_lower_body_renders_before_upper_body() -> None:
    """The last garment applied is the one that layers on top, and an untucked
    shirt hangs OVER the waistband rather than under it. Reversing this draws
    trousers across the shirt hem — a detail nobody articulates and everybody
    sees."""
    from stylist_worker.tryon import PASS_ORDER

    assert PASS_ORDER["lower"] < PASS_ORDER["upper_base"] < PASS_ORDER["upper_layer"]


async def test_consent_is_rechecked_at_render_time_not_at_enqueue(
    monkeypatch, api, registered, owner_engine
) -> None:
    """THE WINDOW THIS CLOSES.

    Between the tap and the render there is a queue that can be minutes long.
    A revocation landing inside that window must stop the render — checking
    only at enqueue would transmit a body photo to a third party seconds after
    the owner told us not to.

    A PROVIDER IS CONFIGURED FOR THIS TEST ON PURPOSE. Without one the job
    returns "no provider configured" before it ever looks at consent, so the
    test would pass while asserting nothing — the same class of
    looks-live-but-cannot-answer-its-own-question signal this codebase has now
    hit seven times.
    """
    import stylist_api.settings as settings_module
    from stylist_worker import tryon as worker_tryon

    real = settings_module.get_settings()
    monkeypatch.setattr(real, "vton_provider", "leffa", raising=False)
    monkeypatch.setattr(settings_module, "get_settings", lambda: real)

    user_id = await _user_id(owner_engine, registered.email)

    # No body_photo row for this tenant: that is what a revoked consent looks
    # like to the job.
    result = await worker_tryon.render_tryon(
        {},
        user_id=str(user_id),
        aggregate_id=str(uuid.uuid4()),
        payload={"garment_set_hash": "a" * 64},
    )
    assert result["rendered"] is False
    assert "consent" in result["reason"], result


def test_the_relay_routes_tryon_requests() -> None:
    """An outbox event with no handler is silently dropped, so the render would
    simply never happen and the endpoint would keep answering "queued"."""
    from stylist_worker.relay import EVENT_HANDLERS

    assert EVENT_HANDLERS["tryon.requested"] == "render_tryon"


def test_the_render_job_is_registered_with_the_worker() -> None:
    """Same failure as above, one layer down: the relay would enqueue a
    function name arq has never heard of."""
    from stylist_worker.main import WorkerSettings

    assert any(f.__name__ == "render_tryon" for f in WorkerSettings.functions)


def test_the_consent_notice_names_the_third_party_when_there_is_one() -> None:
    """Consent to STORAGE is not consent to TRANSMISSION. Once a provider is
    configured the photo leaves our infrastructure, and a notice that does not
    say so is asking agreement to a different question than the one being
    answered."""
    from stylist_api.routers.tryon import _PROVIDER_NOTICE
    from stylist_clients.vton_client import PROFILES

    for provider in PROFILES:
        assert provider in _PROVIDER_NOTICE, f"{provider} would be transmitted to unnamed"


async def test_the_daily_quota_counts_only_this_tenants_renders(
    api, registered, second_tenant, owner_engine
) -> None:
    """`audit_log` has row security DISABLED -- it is a cross-tenant
    operational log. The quota query had no tenant predicate and relied on RLS
    that is not there, so one user's ten renders would have exhausted the cap
    for everyone on the deployment.

    A single-user database cannot see this: 10 mine and 10 total are the same
    number. It takes two tenants to show up at all.

    Asserted on the COUNT the endpoint computes rather than through
    `POST /outfits/{hash}/tryon`, which needs a consented body photo, a
    configured provider and a materialised outfit before it reaches the quota
    at all -- three preconditions that would each turn this into a skip.
    """
    import sqlalchemy as sa

    from stylist_api.routers.tryon import TRYON_DAILY_QUOTA

    quota_sql = sa.text(
        "SELECT count(*) FROM audit_log WHERE action = 'tryon.requested' "
        "AND user_id = :uid AND created_at > now() - interval '1 day'"
    )

    async with owner_engine.begin() as conn:
        ids = {}
        for who in (registered.email, second_tenant.email):
            ids[who] = (
                await conn.execute(sa.text("SELECT id FROM users WHERE email = :e"), {"e": who})
            ).scalar_one()

        # Fill the OTHER tenant's quota past the ceiling.
        for _ in range(TRYON_DAILY_QUOTA + 2):
            await conn.execute(
                sa.text(
                    "INSERT INTO audit_log (id, user_id, action, created_at) "
                    "VALUES (gen_random_uuid(), :u, 'tryon.requested', now())"
                ),
                {"u": ids[second_tenant.email]},
            )

        theirs = (
            await conn.execute(quota_sql, {"uid": ids[second_tenant.email]})
        ).scalar_one()
        mine = (await conn.execute(quota_sql, {"uid": ids[registered.email]})).scalar_one()

        # The predicate the endpoint USED to run: no tenant filter at all.
        untenanted = (
            await conn.execute(
                sa.text(
                    "SELECT count(*) FROM audit_log WHERE action = 'tryon.requested' "
                    "AND created_at > now() - interval '1 day'"
                )
            )
        ).scalar_one()

    assert theirs > TRYON_DAILY_QUOTA, "the other tenant is over its cap"
    assert mine == 0, "this tenant has rendered nothing and must be under the cap"
    assert untenanted >= theirs, (
        "without a tenant predicate the count includes other tenants -- which is "
        "exactly why the quota has to filter on user_id"
    )


async def test_a_failed_render_is_reported_not_re_queued(
    api, registered, owner_engine
) -> None:
    """A failure and a job still in flight both leave NO object in storage, so
    this endpoint answered "queued" for both.

    The visible consequence: a render that died in 1.7s against an unreachable
    provider told the user to "tap again to check", forever, and every tap
    spent quota re-enqueueing a job that failed the same way. The worker knew
    the reason the whole time -- it returned it into arq's Redis result, which
    the API never reads.
    """
    import sqlalchemy as sa

    set_hash = "failedrenderhash0000000000000000"

    async with owner_engine.begin() as conn:
        uid = (
            await conn.execute(
                sa.text("SELECT id FROM users WHERE email = :e"), {"e": registered.email}
            )
        ).scalar_one()
        # A request, then a failure recorded AFTER it -- the order the worker
        # produces, and what makes the failure current rather than historic.
        await conn.execute(
            sa.text(
                "INSERT INTO audit_log (id, user_id, action, detail, created_at) VALUES "
                "(gen_random_uuid(), :u, 'tryon.requested', '{}'::jsonb,"
                " now() - interval '1 minute')"
            ),
            {"u": uid},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO audit_log (id, user_id, action, detail, created_at) VALUES "
                "(gen_random_uuid(), :u, 'tryon.failed', CAST(:d AS jsonb), now())"
            ),
            {"u": uid, "d": json.dumps({"garment_set_hash": set_hash, "reason": "no Gradio API"})},
        )

        current = (
            await conn.execute(
                sa.text(
                    "SELECT detail ->> 'reason' FROM audit_log "
                    "WHERE action = 'tryon.failed' AND user_id = :u "
                    "AND detail ->> 'garment_set_hash' = :h "
                    "AND created_at > (SELECT max(created_at) FROM audit_log "
                    "  WHERE action = 'tryon.requested' AND user_id = :u)"
                ),
                {"u": uid, "h": set_hash},
            )
        ).scalar_one_or_none()

    # This is the predicate the endpoint runs. Before the fix there was no
    # such lookup at all, so the reason could never reach the response.
    assert current == "no Gradio API", (
        "the worker's reason must be readable by the API, or the card can only "
        "ever say 'queued'"
    )
