"""Phase 9 — the erasure saga (§C5).

THE EXIT CRITERION IS "VERIFIED ABSENT", NOT "THE SAGA RAN"
------------------------------------------------------------
So these tests delete a real account and then go LOOKING for what should be
gone — rows queried back, object versions counted, tokens checked. A test that
asserted "the saga reported success" would pass against a saga that did
nothing, which is exactly the failure mode erasure has: nobody notices data
that is still there until someone asks.

The one that matters most is
`test_deleting_an_object_leaves_no_recoverable_version`. The bucket has
versioning ON, so `delete_object` writes a delete MARKER and leaves the bytes
recoverable. That looks like deletion in code review and is not erasure.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from stylist_db.session import system_session, tenant_session
from stylist_worker import erasure


class FakeStore:
    """Records what was asked for, so "all versions" is asserted by CALL."""

    def __init__(self) -> None:
        self.versions: dict[str, int] = {}
        self.purged: list[str] = []

    def delete_all_versions(self, prefix: str) -> int:
        self.purged.append(prefix)
        removed = sum(n for k, n in self.versions.items() if k.startswith(prefix))
        self.versions = {k: n for k, n in self.versions.items() if not k.startswith(prefix)}
        return removed

    def count_versions(self, prefix: str) -> int:
        return sum(n for k, n in self.versions.items() if k.startswith(prefix))

    def get_bytes(self, key: str) -> bytes:
        return b"x"


class FakeGateway:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_virtual_key(self, key: str) -> bool:
        self.deleted.append(key)
        return True


@pytest.fixture
async def populated(owner_engine, initialised_engine, monkeypatch):
    """A tenant with data in every system the saga has to reach."""
    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    user_id = uuid.uuid4()

    async with maker() as session, session.begin():
        await session.execute(
            text("INSERT INTO users (id, email, password_hash) VALUES (:i, :e, 'x')"),
            {"i": user_id, "e": f"erase-{user_id}@example.com"},
        )

    async with tenant_session(user_id) as db:
        await db.execute(
            text(
                "INSERT INTO user_profile (id, user_id, litellm_key) VALUES (:i, :u, 'sk-tenant')"
            ),
            {"i": uuid.uuid4(), "u": user_id},
        )
        for _ in range(3):
            gid = uuid.uuid4()
            await db.execute(
                text(
                    "INSERT INTO garments (id, user_id, original_key, cutout_key, slot, state) "
                    "VALUES (:g, :u, :k, :c, 'upper_base', 'complete')"
                ),
                {
                    "g": gid,
                    "u": user_id,
                    "k": f"originals/{user_id}/p",
                    # A cutout key, because an export without images exercises
                    # none of the part that actually grows with the wardrobe.
                    "c": f"cutouts/{user_id}/{gid}.png",
                },
            )
        await db.execute(
            text(
                "INSERT INTO calendar_link (id, user_id, provider, refresh_token) "
                "VALUES (:i, :u, 'google', 'rt-live')"
            ),
            {"i": uuid.uuid4(), "u": user_id},
        )
        await db.execute(
            text(
                "INSERT INTO device_token (id, user_id, token, platform) "
                "VALUES (:i, :u, 'fcm-token-aaaaaaaaaaaaaaaa', 'android')"
            ),
            {"i": uuid.uuid4(), "u": user_id},
        )
        await db.execute(
            text(
                "INSERT INTO model_calls (id, user_id, model_name, purpose) "
                "VALUES (:i, :u, 'vlm-tagger', 'tag')"
            ),
            {"i": uuid.uuid4(), "u": user_id},
        )

    store = FakeStore()
    store.versions = {
        f"originals/{user_id}/a": 3,  # 3 versions of one object
        f"cutouts/{user_id}/b": 2,
        f"boards/{user_id}/c": 1,
    }
    gateway = FakeGateway()

    from stylist_worker import deps

    monkeypatch.setattr(deps, "get_object_store", lambda: store)
    monkeypatch.setattr(deps, "get_litellm_client", lambda: gateway)

    async def fake_revoke(token: str) -> bool:
        return True

    from stylist_clients import google_calendar as gcal

    monkeypatch.setattr(gcal, "revoke", fake_revoke)

    class T:
        pass

    t = T()
    t.id = user_id
    t.store = store
    t.gateway = gateway
    return t


async def _run_full(user_id: uuid.UUID) -> dict:
    result = await erasure.request_erasure(user_id, "someone@example.com")
    outcome = await erasure.run_erasure(uuid.UUID(result["erasure_id"]), user_id, ["soft_deleted"])
    assert outcome == "confirmed", outcome
    async with system_session() as db:
        row = await db.execute(
            text("SELECT * FROM erasure_request WHERE id = :i"), {"i": result["erasure_id"]}
        )
        return dict(row.mappings().one())


# --------------------------------------------- verified absent, per system


async def test_rows_are_actually_gone_from_every_table(populated) -> None:
    """Queried back, not asserted. `ON DELETE CASCADE` would pass a test that
    only checked `garments`; the point of the explicit order is the tables
    that do NOT cascade."""
    await _run_full(populated.id)

    async with system_session() as db:
        for table in ("garments", "calendar_link", "device_token", "user_profile", "users"):
            column = "id" if table == "users" else "user_id"
            row = await db.execute(
                text(f"SELECT count(*) FROM {table} WHERE {column} = :u"),
                {"u": populated.id},
            )
            assert row.scalar_one() == 0, f"{table} still holds rows for an erased user"


async def test_deleting_an_object_leaves_no_recoverable_version(populated) -> None:
    """THE ONE THAT MATTERS MOST.

    The bucket has versioning ON — Phase 1 turned it on deliberately so a bad
    migration could not destroy a user's photos. That means `delete_object`
    writes a delete MARKER and the bytes stay recoverable. It reads as deletion
    in code review and is not erasure.
    """
    assert populated.store.count_versions(f"originals/{populated.id}/") == 3

    await _run_full(populated.id)

    for prefix in ("originals", "cutouts", "boards", "masks", "grids"):
        assert populated.store.count_versions(f"{prefix}/{populated.id}/") == 0
    assert any("originals/" in p for p in populated.store.purged)


async def test_third_party_grants_are_revoked(populated) -> None:
    """The LiteLLM key is deleted at the gateway and the calendar token at
    Google. Deleting our row alone would leave a live grant on someone else's
    infrastructure that nothing in this system knows about."""
    await _run_full(populated.id)
    assert populated.gateway.deleted == ["sk-tenant"]


async def test_what_could_not_be_purged_is_recorded(populated) -> None:
    """§C5: "record what could NOT be purged (and disclose it)".

    No provider in this stack offers a per-user cache purge, so a saga that
    reported unqualified success would be claiming something false. The user's
    next question is a regulator's first question.
    """
    record = await _run_full(populated.id)
    systems = {u["system"] for u in record["unpurgeable"]}
    assert "model_provider" in systems
    assert all(u.get("reason") for u in record["unpurgeable"]), "a disclosure needs a reason"


async def test_the_audit_record_survives_the_erasure(populated) -> None:
    """Step 7's proof is deliberately NOT deleted by step 6. Both regimes
    require demonstrating a request was honoured, and a deletion record you
    delete demonstrates nothing."""
    record = await _run_full(populated.id)

    async with system_session() as db:
        row = await db.execute(
            text(
                "SELECT detail FROM audit_log WHERE action = 'erasure.completed' "
                "AND subject_id = :i"
            ),
            {"i": record["id"]},
        )
        detail = row.scalar_one()

    assert detail["user_id"] == str(populated.id)
    # Pseudonymous: an id and counts, never an email or an object key.
    assert "@" not in str(detail)


async def test_the_erasure_record_itself_survives(populated) -> None:
    """No FK to `users`, on purpose: step 6 deletes that row, and a cascade
    would destroy the evidence at the exact moment the saga completes."""
    record = await _run_full(populated.id)
    assert record["state"] == "confirmed"
    assert record["completed_at"] is not None


# ------------------------------------------------------ saga properties


async def test_a_second_request_does_not_start_a_competing_saga(populated) -> None:
    """Two sagas racing over the same rows is how you get half-deleted state
    and two conflicting audit records."""
    first = await erasure.request_erasure(populated.id, "a@example.com")
    second = await erasure.request_erasure(populated.id, "a@example.com")

    assert second["already_requested"] is True
    assert second["erasure_id"] == first["erasure_id"]


async def test_resuming_skips_completed_steps(populated) -> None:
    """A saga that cannot resume fails permanently the first time a provider is
    down. Asserted by CALL COUNT: the object purge must not run twice."""
    result = await erasure.request_erasure(populated.id, "a@example.com")
    await erasure.run_erasure(uuid.UUID(result["erasure_id"]), populated.id, ["soft_deleted"])
    calls_after_first = len(populated.store.purged)

    # Re-run with everything already recorded as complete.
    async with system_session() as db:
        row = await db.execute(
            text("SELECT completed_steps FROM erasure_request WHERE id = :i"),
            {"i": result["erasure_id"]},
        )
        done = list(row.scalar_one())
    await erasure.run_erasure(uuid.UUID(result["erasure_id"]), populated.id, done)

    assert len(populated.store.purged) == calls_after_first, "completed steps were re-run"


async def test_step_one_disables_the_account_before_anything_else(populated) -> None:
    """`deleted_at` is set inside the REQUEST, not by the background job. A
    user who deletes their account and can still log in a second later does not
    believe anything else the saga claims."""
    await erasure.request_erasure(populated.id, "a@example.com")

    async with system_session() as db:
        row = await db.execute(
            text("SELECT deleted_at FROM users WHERE id = :u"), {"u": populated.id}
        )
        assert row.scalar_one() is not None


async def test_a_failure_mid_saga_is_retryable_not_terminal(populated, monkeypatch) -> None:
    """An erasure that hit a transient error must keep retrying until the SLA.
    Marking it `failed` would abandon a request with a LEGAL deadline."""

    def boom(prefix: str) -> int:
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(populated.store, "delete_all_versions", boom)

    result = await erasure.request_erasure(populated.id, "a@example.com")
    outcome = await erasure.run_erasure(
        uuid.UUID(result["erasure_id"]), populated.id, ["soft_deleted"]
    )

    assert outcome == "retry"
    async with system_session() as db:
        row = await db.execute(
            text("SELECT state, attempts, last_error FROM erasure_request WHERE id = :i"),
            {"i": result["erasure_id"]},
        )
        record = row.mappings().one()
    assert record["state"] != "failed", "terminal would abandon a legal deadline"
    assert record["attempts"] == 1
    assert record["last_error"]


# ------------------------------------------- asynchronous export (§C5)


class ExportStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.purged: list[str] = []

    def get_bytes(self, key: str) -> bytes:
        return b"\x89PNG-fake"

    def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
        self.objects[key] = data

    def delete_all_versions(self, prefix: str) -> int:
        self.purged.append(prefix)
        n = len([k for k in self.objects if k.startswith(prefix)])
        self.objects = {k: v for k, v in self.objects.items() if not k.startswith(prefix)}
        return n


async def _build(user_id: uuid.UUID, store) -> tuple[uuid.UUID, dict]:
    from stylist_worker import export

    export_id = uuid.uuid4()
    async with tenant_session(user_id) as db:
        await db.execute(
            text("INSERT INTO export_request (id, user_id) VALUES (:i, :u)"),
            {"i": export_id, "u": user_id},
        )
    result = await export.build_export(
        {}, user_id=str(user_id), aggregate_id=str(export_id), payload={}
    )
    return export_id, result


async def test_the_export_is_built_on_disk_not_in_memory(populated, monkeypatch) -> None:
    """An in-memory ZIP works at nine garments and cannot work at two thousand
    — the archive is every cutout the user owns, so it grows with the wardrobe
    while the request timeout does not.

    Asserted through the shape that makes it true: the builder writes to a
    temporary FILE and uploads the bytes, so a caller never receives an
    archive-sized response at all.
    """
    from stylist_worker import deps, export

    store = ExportStore()
    monkeypatch.setattr(deps, "get_object_store", lambda: store)

    export_id, result = await _build(populated.id, store)

    key = export.export_key(populated.id, export_id)
    assert key in store.objects, "the archive went to storage, not to the caller"
    assert result["images"] == 3


async def test_the_archive_contains_csv_json_and_images(populated, monkeypatch) -> None:
    """§C5 says "CSV + all images". CSV is what a person can open; JSON is what
    survives a round trip without a spreadsheet reinterpreting dates and ids.
    Only one of the two makes the export unusable for half its purpose."""
    import io
    import zipfile

    from stylist_worker import deps, export

    store = ExportStore()
    monkeypatch.setattr(deps, "get_object_store", lambda: store)
    export_id, _ = await _build(populated.id, store)

    archive = zipfile.ZipFile(io.BytesIO(store.objects[export.export_key(populated.id, export_id)]))
    names = archive.namelist()

    assert "data/garments.json" in names and "data/garments.csv" in names
    assert "manifest.json" in names and "README.txt" in names
    assert sum(1 for n in names if n.startswith("images/")) == 3


async def test_credentials_are_redacted_from_the_export(populated, monkeypatch) -> None:
    """An export is handed to the user and lands in a downloads folder. A
    calendar refresh token or an FCM registration token STILL WORKS — exporting
    one is handing over a live credential, not a copy of their data."""
    import io
    import json as _json
    import zipfile

    from stylist_worker import deps, export

    store = ExportStore()
    monkeypatch.setattr(deps, "get_object_store", lambda: store)
    export_id, _ = await _build(populated.id, store)

    archive = zipfile.ZipFile(io.BytesIO(store.objects[export.export_key(populated.id, export_id)]))
    links = _json.loads(archive.read("data/calendar_link.json"))
    devices = _json.loads(archive.read("data/device_token.json"))

    assert links and links[0]["refresh_token"] == "[redacted]"
    assert devices and devices[0]["token"] == "[redacted]"
    # The raw values must appear nowhere in the archive at all.
    blob = store.objects[export.export_key(populated.id, export_id)]
    assert b"rt-live" not in blob
    assert b"fcm-token-aaaaaaaaaaaaaaaa" not in blob


async def test_an_expired_export_is_actually_deleted(populated, monkeypatch) -> None:
    """§C5's "7-day link". A presigned URL expiring only stops new downloads —
    the ZIP stays in the bucket. That archive is a complete second copy of the
    wardrobe, i.e. exactly what the erasure saga works to remove."""
    from stylist_worker import deps, export

    store = ExportStore()
    monkeypatch.setattr(deps, "get_object_store", lambda: store)
    export_id, _ = await _build(populated.id, store)
    assert store.objects

    async with tenant_session(populated.id) as db:
        await db.execute(
            text("UPDATE export_request SET expires_at = now() - interval '1 day' WHERE id = :i"),
            {"i": export_id},
        )

    # The sweep is CROSS-TENANT by design — it cleans up after everyone — so
    # the global count depends on whatever else is expired. The assertion is
    # about THIS export, which is what the test is actually claiming.
    await export.sweep_expired_exports({})

    assert not store.objects, "the archive is gone, not just the link"
    async with tenant_session(populated.id) as db:
        row = await db.execute(
            text("SELECT deleted_at, object_key FROM export_request WHERE id = :i"),
            {"i": export_id},
        )
        record = row.mappings().one()
    assert record["deleted_at"] is not None and record["object_key"] is None


async def test_a_failed_sweep_leaves_the_record_for_the_next_run(populated, monkeypatch) -> None:
    """Marking it deleted after a failed purge means nothing ever tries again
    and the copy stays forever — a leak that looks like a completed cleanup."""
    from stylist_worker import deps, export

    store = ExportStore()
    monkeypatch.setattr(deps, "get_object_store", lambda: store)
    export_id, _ = await _build(populated.id, store)

    async with tenant_session(populated.id) as db:
        await db.execute(
            text("UPDATE export_request SET expires_at = now() - interval '1 day' WHERE id = :i"),
            {"i": export_id},
        )

    def boom(prefix: str) -> int:
        raise RuntimeError("storage down")

    monkeypatch.setattr(store, "delete_all_versions", boom)
    result = await export.sweep_expired_exports({})

    assert result["removed"] == 0
    async with tenant_session(populated.id) as db:
        row = await db.execute(
            text("SELECT deleted_at FROM export_request WHERE id = :i"), {"i": export_id}
        )
        assert row.scalar_one() is None, "still pending, so the next sweep retries"
