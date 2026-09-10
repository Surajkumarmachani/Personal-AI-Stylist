"""Phase 4: VLM tagging, the degrade ladder, and corrections.

The exit criteria this file pins:

  - 6 garments = 1 VLM call (asserted by COUNTING calls, not by inspection)
  - provider down -> DEGRADED_TAGGED, garment still usable
  - budget exhausted -> cataloguing still works, no retry storm
  - a user correction is never overwritten by a later tag run
  - unknown enum values are dropped per-field, not by discarding the response

The gateway is faked at the client boundary so these run without LiteLLM, a
provider, or credentials. What is NOT faked is the schema, the parser, the SQL
and the taxonomy — the parts that decide whether a response becomes a correct
row.
"""

from __future__ import annotations

import io
import json
import uuid
from typing import Any

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import text

from stylist_domain.taxonomy import load_taxonomy
from stylist_domain.vlm_schema import build_prompt, build_schema, required_fields, vlm_fields
from stylist_worker.grid import MAX_CELLS, cell_name, compose


def cutout(colour: tuple[int, int, int] = (123, 31, 43)) -> bytes:
    img = Image.new("RGBA", (300, 400), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle([40, 60, 260, 340], radius=20, fill=(*colour, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put(self, key: str, data: bytes) -> None:
        self.objects[key] = (data, "image/png")

    def head(self, key: str) -> dict[str, Any] | None:
        return {"ContentLength": len(self.objects[key][0])} if key in self.objects else None

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key][0]

    def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
        self.objects[key] = (data, content_type)


class FakeGateway:
    """Records every call so batching can be asserted by COUNT."""

    def __init__(self, *, response: str | None = None, raises: Exception | None = None):
        self.calls: list[dict[str, Any]] = []
        self._response = response
        self._raises = raises

    async def chat(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        from stylist_clients.litellm_client import ChatResult

        return ChatResult(
            content=self._response or json.dumps({"items": []}),
            model="fake-vlm",
            prompt_tokens=1000,
            completion_tokens=200,
            cost_usd=0.00042,
            latency_ms=900,
            cache_hit=False,
        )


def tag_payload(cells: list[str], *, material_confidence: float = 0.9) -> str:
    return json.dumps(
        {
            "items": [
                {
                    "cell": cell,
                    "subcategory": "kurta",
                    "material": "cotton",
                    "formality": 3,
                    "dress_code": "festive_ethnic",
                    "warmth": 2,
                    "fit": "relaxed",
                    "confidence": {
                        "subcategory": 0.9,
                        "material": material_confidence,
                        "formality": 0.9,
                        "dress_code": 0.9,
                        "warmth": 0.9,
                        "fit": 0.9,
                    },
                }
                for cell in cells
            ]
        }
    )


async def seed_garments(owner_engine, count: int) -> tuple[uuid.UUID, uuid.UUID, list[str]]:
    """A user with `count` garments that already have cutouts."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    user_id, job_id = uuid.uuid4(), uuid.uuid4()
    original = f"originals/{user_id}/photo"
    garment_ids: list[str] = []

    async with maker() as session, session.begin():
        await session.execute(
            text("INSERT INTO users (id, email, password_hash) VALUES (:id, :e, 'x')"),
            {"id": user_id, "e": f"tag-{user_id}@example.com"},
        )
        await session.execute(
            text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(user_id)}
        )
        await session.execute(
            text(
                "INSERT INTO user_profile (id, user_id, litellm_key) "
                "VALUES (:id, :uid, 'sk-tenant-key')"
            ),
            {"id": uuid.uuid4(), "uid": user_id},
        )
        for _ in range(count):
            gid = uuid.uuid4()
            garment_ids.append(str(gid))
            await session.execute(
                text(
                    "INSERT INTO garments (id, user_id, original_key, slot, "
                    "primary_colour, cutout_key, state) "
                    "VALUES (:g, :uid, :orig, 'upper_base', 'maroon', :cut, 'classified')"
                ),
                {"g": gid, "uid": user_id, "orig": original, "cut": f"cutouts/{user_id}/{gid}.png"},
            )
        await session.execute(
            text(
                "INSERT INTO jobs (id, user_id, kind, state, garment_id, payload) "
                "VALUES (:j, :uid, 'ingest', 'classified', :g, CAST(:p AS jsonb))"
            ),
            {
                "j": job_id,
                "uid": user_id,
                "g": uuid.UUID(garment_ids[0]),
                "p": json.dumps({"key": original}),
            },
        )
    return user_id, job_id, garment_ids


def make_ctx(user_id: uuid.UUID, job_id: uuid.UUID, garment_id: str, scratch: dict[str, Any]):
    from stylist_worker.state_machine import JobContext

    return JobContext(
        job_id=job_id,
        user_id=user_id,
        garment_id=uuid.UUID(garment_id),
        payload={"key": f"originals/{user_id}/photo"},
        state="classified",
        scratch=scratch,
    )


async def cleanup(owner_engine, user_id: uuid.UUID) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})


# ------------------------------------------------------------ schema


def test_schema_enums_come_from_the_taxonomy() -> None:
    """A model returning a bad value must be a SCHEMA violation, caught
    mechanically — not a bad row discovered weeks later when no filter matches
    it."""
    taxonomy = load_taxonomy()
    schema = build_schema(taxonomy, cells=["A1", "A2"])
    props = schema["json_schema"]["schema"]["properties"]["items"]["items"]["properties"]

    assert props["subcategory"]["enum"] == list(taxonomy.subcategories)
    assert props["material"]["enum"] == list(taxonomy.materials)
    assert props["dress_code"]["enum"] == list(taxonomy.dress_codes)
    assert props["fit"]["enum"] == list(taxonomy.fits)
    assert props["cell"]["enum"] == ["A1", "A2"]
    assert schema["json_schema"]["strict"] is True


def test_only_vlm_tier_fields_are_requested() -> None:
    """Asking for slot, colour or pattern would pay tokens for answers local
    extraction already has — and let a costlier, less reliable source
    contradict a cheaper one."""
    taxonomy = load_taxonomy()
    requested = set(vlm_fields(taxonomy))
    assert "slot" not in requested, "slot comes from segmentation, for free"
    assert "primary_colour" not in requested, "colour comes from CIELAB k-means"
    assert "pattern" not in requested
    assert "climate_bands" not in requested, "climate_bands is tier=rule, derived"
    assert requested == {"subcategory", "material", "formality", "dress_code", "warmth", "fit"}


def test_required_fields_track_the_taxonomy() -> None:
    taxonomy = load_taxonomy()
    required = set(required_fields(taxonomy))
    # material and fit are required: false — from a photo they are genuinely
    # ambiguous, and demanding them invites confident guesses.
    assert "material" not in required
    assert "fit" not in required
    assert {"subcategory", "formality", "dress_code", "warmth"} <= required


def test_prompt_carries_local_hints_so_they_cannot_be_contradicted() -> None:
    taxonomy = load_taxonomy()
    prompt = build_prompt(taxonomy, cells=["A1"], hints={"A1": "slot=lower, colour=denim_indigo"})
    assert "slot=lower" in prompt
    assert "do NOT contradict" in prompt
    # The value sets are India-specific and the model has to be told, or it
    # collapses festive_ethnic onto "formal".
    assert "saree" in prompt or "lehenga" in prompt
    assert "16-34C" in prompt


# -------------------------------------------------------------- grid


def test_six_garments_compose_into_one_grid() -> None:
    """The batching claim, at the composition layer: 6 cutouts -> ONE image."""
    grid = compose([(f"g{i}", cutout()) for i in range(6)])
    assert len(grid.cells) == 6
    assert grid.cells == ["A1", "A2", "A3", "B1", "B2", "B3"]
    with Image.open(io.BytesIO(grid.png)) as img:
        assert img.size[0] > 0


def test_cells_map_back_to_garments_unambiguously() -> None:
    """Keying by cell rather than position is what stops one dropped item from
    shifting every subsequent garment's tags onto the wrong garment."""
    ids = [str(uuid.uuid4()) for _ in range(4)]
    grid = compose([(gid, cutout()) for gid in ids])
    assert list(grid.cell_to_garment.values()) == ids
    assert grid.cell_to_garment[cell_name(0)] == ids[0]


def test_the_cell_cap_is_enforced() -> None:
    """Beyond 6, each cell shrinks below what the model can identify — cost
    saved is repaid immediately in corrections."""
    with pytest.raises(ValueError, match="exceeds"):
        compose([(f"g{i}", cutout()) for i in range(MAX_CELLS + 1)])


# ------------------------------------------------ batching + writes


async def test_six_garments_cost_exactly_one_vlm_call(owner_engine) -> None:
    """PHASE 4 EXIT CRITERION: 6 garments = 1 VLM call, asserted by count.

    Six separate calls would pay the per-request overhead and re-send the
    instruction text six times. §B2 puts VLM volume at ~0.02 RPS, so batching
    buys nothing in throughput and roughly 6x in cost — the entire reason to
    do it.
    """
    from stylist_worker.stages.tag import tag_stage

    user_id, job_id, garment_ids = await seed_garments(owner_engine, 6)
    store = FakeStore()
    for gid in garment_ids:
        store.put(f"cutouts/{user_id}/{gid}.png", cutout())
    gateway = FakeGateway(response=tag_payload(["A1", "A2", "A3", "B1", "B2", "B3"]))

    try:
        result = await tag_stage.run(
            make_ctx(user_id, job_id, garment_ids[0], {"store": store, "litellm": gateway})
        )
        assert len(gateway.calls) == 1, f"expected 1 call for 6 garments, made {len(gateway.calls)}"
        assert result["tagged"] == 6
        assert result["vlm_calls"] == 1
    finally:
        await cleanup(owner_engine, user_id)


async def test_seven_garments_take_two_calls_not_seven(owner_engine) -> None:
    from stylist_worker.stages.tag import tag_stage

    user_id, job_id, garment_ids = await seed_garments(owner_engine, 7)
    store = FakeStore()
    for gid in garment_ids:
        store.put(f"cutouts/{user_id}/{gid}.png", cutout())
    gateway = FakeGateway(response=tag_payload(["A1", "A2", "A3", "B1", "B2", "B3"]))

    try:
        await tag_stage.run(
            make_ctx(user_id, job_id, garment_ids[0], {"store": store, "litellm": gateway})
        )
        assert len(gateway.calls) == 2, "7 garments should batch as 6 + 1"
    finally:
        await cleanup(owner_engine, user_id)


async def test_tags_are_written_and_the_review_gate_fires(owner_engine) -> None:
    """A field below its taxonomy `review_below` threshold flags the garment.

    Verified end-to-end against the live stack too: material at 0.55 flagged
    one garment and left another (0.88) alone.
    """
    from stylist_worker.stages.tag import tag_stage

    user_id, job_id, garment_ids = await seed_garments(owner_engine, 1)
    store = FakeStore()
    store.put(f"cutouts/{user_id}/{garment_ids[0]}.png", cutout())
    # material's review_below is 0.60.
    gateway = FakeGateway(response=tag_payload(["A1"], material_confidence=0.40))

    from sqlalchemy.ext.asyncio import async_sessionmaker

    try:
        await tag_stage.run(
            make_ctx(user_id, job_id, garment_ids[0], {"store": store, "litellm": gateway})
        )
        maker = async_sessionmaker(owner_engine, expire_on_commit=False)
        async with maker() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT subcategory, material, formality, dress_code, warmth, "
                            "fit, needs_review, extractor_version, field_confidence "
                            "FROM garments WHERE id = :g"
                        ),
                        {"g": uuid.UUID(garment_ids[0])},
                    )
                )
                .mappings()
                .one()
            )
        assert row["subcategory"] == "kurta"
        assert row["material"] == "cotton"
        assert row["formality"] == 3
        assert row["dress_code"] == "festive_ethnic"
        assert row["needs_review"] is True, "a sub-threshold confidence must flag review"
        assert row["extractor_version"]
        assert row["field_confidence"]["material"] == 0.40
    finally:
        await cleanup(owner_engine, user_id)


async def test_the_raw_response_is_stored_for_reprocessing(owner_engine) -> None:
    """§D1: reprocessing must NEVER require re-calling the provider.

    Without the raw response, a prompt or parser fix becomes a second invoice
    for every garment already ingested.
    """
    from stylist_worker.stages.tag import tag_stage

    user_id, job_id, garment_ids = await seed_garments(owner_engine, 1)
    store = FakeStore()
    store.put(f"cutouts/{user_id}/{garment_ids[0]}.png", cutout())
    gateway = FakeGateway(response=tag_payload(["A1"]))

    from sqlalchemy.ext.asyncio import async_sessionmaker

    try:
        await tag_stage.run(
            make_ctx(user_id, job_id, garment_ids[0], {"store": store, "litellm": gateway})
        )
        maker = async_sessionmaker(owner_engine, expire_on_commit=False)
        async with maker() as session:
            raw = (
                await session.execute(
                    text("SELECT attributes_raw->'tag' FROM garments WHERE id = :g"),
                    {"g": uuid.UUID(garment_ids[0])},
                )
            ).scalar_one()
        assert raw["item"]["subcategory"] == "kurta"
        assert raw["model"] == "fake-vlm"
        assert raw["extractor_version"]
    finally:
        await cleanup(owner_engine, user_id)


async def test_spend_is_mirrored_into_model_calls(owner_engine) -> None:
    """LiteLLM has its own spend log but cannot answer "cost per garment
    ingested" — it does not know what a garment or a job is. §B3's cost
    governance needs money joined to our domain."""
    from stylist_worker.stages.tag import tag_stage

    user_id, job_id, garment_ids = await seed_garments(owner_engine, 2)
    store = FakeStore()
    for gid in garment_ids:
        store.put(f"cutouts/{user_id}/{gid}.png", cutout())
    gateway = FakeGateway(response=tag_payload(["A1", "A2"]))

    from sqlalchemy.ext.asyncio import async_sessionmaker

    try:
        await tag_stage.run(
            make_ctx(user_id, job_id, garment_ids[0], {"store": store, "litellm": gateway})
        )
        maker = async_sessionmaker(owner_engine, expire_on_commit=False)
        async with maker() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT model_name, purpose, cost_usd, prompt_tokens, job_id "
                            "FROM model_calls WHERE user_id = :uid"
                        ),
                        {"uid": user_id},
                    )
                )
                .mappings()
                .one()
            )
        assert row["purpose"] == "tag"
        assert float(row["cost_usd"]) == pytest.approx(0.00042)
        assert row["job_id"] == job_id, "cost must join to the job that incurred it"
    finally:
        await cleanup(owner_engine, user_id)


# --------------------------------------------------- degrade ladder


async def test_budget_exhaustion_degrades_instead_of_failing(owner_engine) -> None:
    """PHASE 4 EXIT CRITERION: budget gone -> cataloguing still works.

    §B3's designed outcome. The garment keeps its slot, colour, cutout and
    embedding; only new AI tags stop. No retry, because the budget will not
    refill inside this job's lifetime.
    """
    from stylist_clients.litellm_client import BudgetExhausted
    from stylist_worker.stages.tag import tag_stage
    from stylist_worker.state_machine import IngestState, Terminal

    user_id, job_id, garment_ids = await seed_garments(owner_engine, 2)
    store = FakeStore()
    for gid in garment_ids:
        store.put(f"cutouts/{user_id}/{gid}.png", cutout())
    gateway = FakeGateway(raises=BudgetExhausted("Budget has been exceeded"))

    from sqlalchemy.ext.asyncio import async_sessionmaker

    try:
        with pytest.raises(Terminal) as exc:
            await tag_stage.run(
                make_ctx(user_id, job_id, garment_ids[0], {"store": store, "litellm": gateway})
            )
        assert exc.value.state is IngestState.DEGRADED_TAGGED
        assert "usable" in exc.value.reason

        maker = async_sessionmaker(owner_engine, expire_on_commit=False)
        async with maker() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT slot, primary_colour, cutout_key, "
                            "attributes_raw->'tag'->>'reason' AS reason "
                            "FROM garments WHERE user_id = :uid"
                        ),
                        {"uid": user_id},
                    )
                )
                .mappings()
                .all()
            )
        # The garment is still complete and usable — that IS the requirement.
        for row in rows:
            assert row["slot"] == "upper_base"
            assert row["primary_colour"] == "maroon"
            assert row["cutout_key"]
            assert "budget" in (row["reason"] or "").lower()
        assert len(gateway.calls) == 1, "budget exhaustion must not be retried"
    finally:
        await cleanup(owner_engine, user_id)


async def test_a_gateway_outage_is_backpressure_not_a_lost_tag(owner_engine) -> None:
    """An unreachable gateway must not spend the image's retry budget — the
    same lesson as the ml restart that DLQ'd every in-flight ingest."""
    from stylist_clients.litellm_client import LiteLLMUnavailable
    from stylist_worker.stages.tag import tag_stage
    from stylist_worker.state_machine import Unavailable

    user_id, job_id, garment_ids = await seed_garments(owner_engine, 1)
    store = FakeStore()
    store.put(f"cutouts/{user_id}/{garment_ids[0]}.png", cutout())
    gateway = FakeGateway(raises=LiteLLMUnavailable("ConnectError", retry_after=3.0))

    try:
        with pytest.raises(Unavailable) as exc:
            await tag_stage.run(
                make_ctx(user_id, job_id, garment_ids[0], {"store": store, "litellm": gateway})
            )
        assert exc.value.retry_after == 3.0
    finally:
        await cleanup(owner_engine, user_id)


async def test_a_tenant_without_a_key_degrades_rather_than_blocking(owner_engine) -> None:
    """A gateway outage at signup must not permanently break that user's
    ingest. The key is backfillable; a blocked pipeline is not recoverable
    without intervention."""
    from stylist_worker.stages.tag import tag_stage
    from stylist_worker.state_machine import IngestState, Terminal

    user_id, job_id, garment_ids = await seed_garments(owner_engine, 1)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        await session.execute(
            text("UPDATE user_profile SET litellm_key = NULL WHERE user_id = :uid"),
            {"uid": user_id},
        )

    store = FakeStore()
    store.put(f"cutouts/{user_id}/{garment_ids[0]}.png", cutout())
    gateway = FakeGateway(response=tag_payload(["A1"]))

    try:
        with pytest.raises(Terminal) as exc:
            await tag_stage.run(
                make_ctx(user_id, job_id, garment_ids[0], {"store": store, "litellm": gateway})
            )
        assert exc.value.state is IngestState.DEGRADED_TAGGED
        assert len(gateway.calls) == 0, "no call should be attempted without a key"
    finally:
        await cleanup(owner_engine, user_id)


async def test_unparseable_json_degrades_without_losing_the_garment(owner_engine) -> None:
    from stylist_worker.stages.tag import tag_stage

    user_id, job_id, garment_ids = await seed_garments(owner_engine, 1)
    store = FakeStore()
    store.put(f"cutouts/{user_id}/{garment_ids[0]}.png", cutout())
    gateway = FakeGateway(response="I'm sorry, I can't help with that.")

    from sqlalchemy.ext.asyncio import async_sessionmaker

    try:
        result = await tag_stage.run(
            make_ctx(user_id, job_id, garment_ids[0], {"store": store, "litellm": gateway})
        )
        assert result["tagged"] == 0
        maker = async_sessionmaker(owner_engine, expire_on_commit=False)
        async with maker() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT cutout_key, primary_colour, "
                            "attributes_raw->'tag'->>'reason' AS reason "
                            "FROM garments WHERE id = :g"
                        ),
                        {"g": uuid.UUID(garment_ids[0])},
                    )
                )
                .mappings()
                .one()
            )
        assert row["cutout_key"], "the garment must survive an unusable response"
        assert row["primary_colour"] == "maroon"
        assert "parse" in (row["reason"] or "")
    finally:
        await cleanup(owner_engine, user_id)


async def test_one_bad_enum_value_does_not_discard_the_whole_response(
    owner_engine,
) -> None:
    """Dropping five good fields because `material` came back as "cotton blend"
    would turn a small model error into a total extraction failure."""
    from stylist_worker.stages.tag import tag_stage

    user_id, job_id, garment_ids = await seed_garments(owner_engine, 1)
    store = FakeStore()
    store.put(f"cutouts/{user_id}/{garment_ids[0]}.png", cutout())

    payload = json.loads(tag_payload(["A1"]))
    payload["items"][0]["material"] = "cotton blend"  # not in the taxonomy
    gateway = FakeGateway(response=json.dumps(payload))

    from sqlalchemy.ext.asyncio import async_sessionmaker

    try:
        await tag_stage.run(
            make_ctx(user_id, job_id, garment_ids[0], {"store": store, "litellm": gateway})
        )
        maker = async_sessionmaker(owner_engine, expire_on_commit=False)
        async with maker() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT subcategory, material, formality, "
                            "attributes_raw->'tag'->'dropped' AS dropped "
                            "FROM garments WHERE id = :g"
                        ),
                        {"g": uuid.UUID(garment_ids[0])},
                    )
                )
                .mappings()
                .one()
            )
        assert row["subcategory"] == "kurta", "good fields must survive"
        assert row["formality"] == 3
        assert row["material"] is None, "the bad value must not be stored"
        assert any("material" in d for d in row["dropped"]), "the drop must be recorded"
    finally:
        await cleanup(owner_engine, user_id)


async def test_a_response_naming_an_unsent_cell_is_dropped(owner_engine) -> None:
    """A cell we never sent describes something imagined. Attaching it to a
    real garment would be worse than dropping it."""
    from stylist_worker.stages.tag import tag_stage

    user_id, job_id, garment_ids = await seed_garments(owner_engine, 1)
    store = FakeStore()
    store.put(f"cutouts/{user_id}/{garment_ids[0]}.png", cutout())
    gateway = FakeGateway(response=tag_payload(["A1", "C3"]))  # C3 was never sent

    try:
        result = await tag_stage.run(
            make_ctx(user_id, job_id, garment_ids[0], {"store": store, "litellm": gateway})
        )
        assert result["tagged"] == 1, "only the real cell should be written"
    finally:
        await cleanup(owner_engine, user_id)


# ------------------------------------------------ correction locking


async def test_a_user_correction_is_never_overwritten_by_tagging(owner_engine) -> None:
    """THE RULE THAT MAKES THE CORRECTION UI TRUSTWORTHY (§D1).

    Without it: the user fixes a value, a backfill or a model upgrade runs, and
    their fix silently disappears. They would have no reason to correct
    anything again.
    """
    from stylist_worker.stages.tag import tag_stage

    user_id, job_id, garment_ids = await seed_garments(owner_engine, 1)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(user_id)}
        )
        # The user has already said: this is silk, and it is a saree.
        await session.execute(
            text(
                "UPDATE garments SET material = 'silk', subcategory = 'saree', "
                "user_verified_fields = ARRAY['material','subcategory'] WHERE id = :g"
            ),
            {"g": uuid.UUID(garment_ids[0])},
        )

    store = FakeStore()
    store.put(f"cutouts/{user_id}/{garment_ids[0]}.png", cutout())
    # The model disagrees on both, and agrees on nothing the user set.
    gateway = FakeGateway(response=tag_payload(["A1"]))

    try:
        await tag_stage.run(
            make_ctx(user_id, job_id, garment_ids[0], {"store": store, "litellm": gateway})
        )
        async with maker() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT material, subcategory, formality, dress_code "
                            "FROM garments WHERE id = :g"
                        ),
                        {"g": uuid.UUID(garment_ids[0])},
                    )
                )
                .mappings()
                .one()
            )
        assert row["material"] == "silk", "the user's correction was overwritten"
        assert row["subcategory"] == "saree", "the user's correction was overwritten"
        # Unverified fields SHOULD be filled in — locking must be per-field,
        # not a blanket freeze on the whole garment.
        assert row["formality"] == 3
        assert row["dress_code"] == "festive_ethnic"
    finally:
        await cleanup(owner_engine, user_id)
