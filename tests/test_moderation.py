"""Phase 4.4: the moderation gate.

Its POSITION is the requirement, not just its accuracy. Every stage that could
export pixels comes after it, so a flagged upload never reaches a third party —
which is also why the classifier is self-hosted. Satisfying this gate with a
hosted moderation API would upload the exact images we are refusing to send
anywhere.

The exit criterion "non-garment upload -> quarantined, ZERO outbound calls" is
asserted by counting calls on a fake gateway, not by reading the code.
"""

from __future__ import annotations

import io
import json
import uuid
from typing import Any

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import text

from stylist_ml import registry


def jpeg() -> bytes:
    img = Image.new("RGB", (500, 700), (238, 235, 230))
    ImageDraw.Draw(img).rounded_rectangle([120, 150, 380, 560], radius=30, fill=(123, 31, 43))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
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


class FakeML:
    """Counts calls so "no third-party call was made" is provable."""

    def __init__(self, verdict: str, score: float) -> None:
        self.verdict = verdict
        self.score = score
        self.moderate_calls = 0
        self.segment_calls = 0
        self.matte_calls = 0
        self.embed_calls = 0

    async def moderate(self, *, image_bytes: bytes) -> dict[str, Any]:
        self.moderate_calls += 1
        return {
            "nsfw_score": self.score,
            "verdict": self.verdict,
            "model": "vit_nsfw_detector",
            "thresholds": {"quarantine": 0.9, "review": 0.6},
        }

    async def segment(self, **kwargs: Any) -> Any:
        self.segment_calls += 1
        raise AssertionError("segment must not run for a quarantined image")

    async def matte(self, **kwargs: Any) -> Any:
        self.matte_calls += 1
        raise AssertionError("matte must not run for a quarantined image")


class CountingGateway:
    """Any call here means pixels left the VPC."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("a quarantined image must never reach a provider")


async def seed(owner_engine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    user_id, job_id, garment_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with maker() as session, session.begin():
        await session.execute(
            text("INSERT INTO users (id, email, password_hash) VALUES (:id, :e, 'x')"),
            {"id": user_id, "e": f"mod-{user_id}@example.com"},
        )
        await session.execute(
            text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(user_id)}
        )
        await session.execute(
            text(
                "INSERT INTO garments (id, user_id, original_key, state) "
                "VALUES (:g, :uid, :orig, 'sanitised')"
            ),
            {"g": garment_id, "uid": user_id, "orig": f"originals/{user_id}/p"},
        )
        await session.execute(
            text(
                "INSERT INTO jobs (id, user_id, kind, state, garment_id, payload) "
                "VALUES (:j, :uid, 'ingest', 'sanitised', :g, CAST(:p AS jsonb))"
            ),
            {
                "j": job_id,
                "uid": user_id,
                "g": garment_id,
                "p": json.dumps({"key": f"originals/{user_id}/p"}),
            },
        )
    return user_id, job_id, garment_id


async def cleanup(owner_engine, user_id: uuid.UUID) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        await session.execute(text("DELETE FROM audit_log WHERE user_id = :id"), {"id": user_id})


def _ctx(user_id, job_id, garment_id, scratch):
    from stylist_worker.keys import sanitised_key
    from stylist_worker.state_machine import JobContext

    store = scratch["store"]
    store.put(sanitised_key(user_id, job_id), jpeg())
    return JobContext(
        job_id=job_id,
        user_id=user_id,
        garment_id=garment_id,
        payload={"key": f"originals/{user_id}/p"},
        state="sanitised",
        scratch=scratch,
    )


# ------------------------------------------------------- the model


def test_the_moderation_model_is_self_hosted_and_in_vpc() -> None:
    """A hosted moderation API would upload the exact images this gate exists
    to keep off other infrastructure."""
    assert registry.NSFW.task == "moderate"
    assert registry.NSFW.local_name.endswith(".onnx")
    assert registry.NSFW.sha256, "weights must be pinned like every other model"
    assert "IN-VPC" in registry.NSFW.notes


def test_two_thresholds_not_one() -> None:
    """A single cutoff forces a choice between quarantining innocent photos and
    letting through what it should catch. The middle band exists because the
    base rate is low and a false quarantine is the worse failure."""
    from stylist_ml.moderation import QUARANTINE_THRESHOLD, REVIEW_THRESHOLD

    assert 0.0 < REVIEW_THRESHOLD < QUARANTINE_THRESHOLD < 1.0


@pytest.mark.skipif(not registry.NSFW.available(), reason="NSFW weights absent")
def test_an_ordinary_garment_photo_passes() -> None:
    """The false-positive case, which matters more than the true-positive one:
    telling someone their photo of a jumper was quarantined is a worse product
    failure than asking them to confirm it."""
    from stylist_ml.moderation import moderate
    from stylist_ml.runtime import load

    result = moderate(load(registry.NSFW), jpeg())
    assert result.verdict == "pass", f"a plain garment scored {result.nsfw_score:.3f}"
    assert result.is_quarantined is False


# -------------------------------------------------------- the gate


async def test_a_quarantine_stops_the_pipeline_before_any_export(owner_engine) -> None:
    """PHASE 4 EXIT CRITERION: quarantined, and ZERO outbound calls.

    Proven by counting: the fake ML raises if segment or matte is reached, and
    the fake gateway raises if any provider call is attempted.
    """
    from stylist_worker.stages.moderate import moderate_stage
    from stylist_worker.state_machine import IngestState, Terminal

    user_id, job_id, garment_id = await seed(owner_engine)
    ml = FakeML("quarantine", 0.97)
    gateway = CountingGateway()
    ctx = _ctx(user_id, job_id, garment_id, {"store": FakeStore(), "ml": ml, "litellm": gateway})

    try:
        with pytest.raises(Terminal) as exc:
            await moderate_stage.run(ctx)
        assert exc.value.state is IngestState.QUARANTINED
        assert ml.moderate_calls == 1
        assert ml.segment_calls == 0
        assert ml.matte_calls == 0
        assert gateway.calls == 0, "a quarantined image reached a provider"
    finally:
        await cleanup(owner_engine, user_id)


async def test_a_quarantine_is_audited_without_recording_anything_personal(
    owner_engine,
) -> None:
    """audit_log survives the erasure saga (legally required), so it must hold
    only a pseudonymous id, a score and a timestamp — no image, no key."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from stylist_worker.stages.moderate import moderate_stage
    from stylist_worker.state_machine import Terminal

    user_id, job_id, garment_id = await seed(owner_engine)
    ctx = _ctx(
        user_id,
        job_id,
        garment_id,
        {"store": FakeStore(), "ml": FakeML("quarantine", 0.99)},
    )
    try:
        with pytest.raises(Terminal):
            await moderate_stage.run(ctx)

        maker = async_sessionmaker(owner_engine, expire_on_commit=False)
        async with maker() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT action, detail FROM audit_log "
                            "WHERE user_id = :uid ORDER BY created_at DESC LIMIT 1"
                        ),
                        {"uid": user_id},
                    )
                )
                .mappings()
                .one()
            )
        assert row["action"] == "moderation.quarantined"
        assert row["detail"]["nsfw_score"] > 0.9
        assert row["detail"]["model"] == "vit_nsfw_detector"
        # Nothing that identifies the image or the person beyond the id.
        forbidden = {"image", "key", "email", "cutout", "bytes", "url"}
        assert not (forbidden & set(row["detail"])), f"audit detail leaks: {row['detail']}"
    finally:
        await cleanup(owner_engine, user_id)


async def test_the_review_band_flags_without_blocking(owner_engine) -> None:
    """Between the thresholds the item is processed AND flagged. Blocking here
    would quarantine ordinary photos; ignoring it would waste the signal."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from stylist_worker.stages.moderate import moderate_stage

    user_id, job_id, garment_id = await seed(owner_engine)
    ctx = _ctx(user_id, job_id, garment_id, {"store": FakeStore(), "ml": FakeML("review", 0.72)})
    try:
        result = await moderate_stage.run(ctx)  # does NOT raise
        assert result["moderation"]["verdict"] == "review"

        maker = async_sessionmaker(owner_engine, expire_on_commit=False)
        async with maker() as session:
            row = (
                (
                    await session.execute(
                        text("SELECT needs_review, moderation FROM garments WHERE id = :g"),
                        {"g": garment_id},
                    )
                )
                .mappings()
                .one()
            )
        assert row["needs_review"] is True
        assert row["moderation"]["verdict"] == "review"
    finally:
        await cleanup(owner_engine, user_id)


async def test_an_unavailable_moderator_waits_and_never_passes(owner_engine) -> None:
    """FAIL CLOSED.

    Treating an unavailable moderator as "probably fine" would let unmoderated
    pixels through during any ml restart — precisely the window in which a gate
    is most likely to be bypassed. Waiting costs latency; skipping costs the
    guarantee.
    """
    from stylist_clients.ml_client import MLUnavailable
    from stylist_worker.stages.moderate import moderate_stage
    from stylist_worker.state_machine import Unavailable

    class DownML:
        async def moderate(self, **kwargs: Any) -> Any:
            raise MLUnavailable("ConnectError", retry_after=2.0)

    user_id, job_id, garment_id = await seed(owner_engine)
    ctx = _ctx(user_id, job_id, garment_id, {"store": FakeStore(), "ml": DownML()})
    try:
        with pytest.raises(Unavailable) as exc:
            await moderate_stage.run(ctx)
        assert exc.value.retry_after == 2.0
    finally:
        await cleanup(owner_engine, user_id)


async def test_a_passing_verdict_is_recorded_on_the_garment(owner_engine) -> None:
    """Kept on the garment rather than only in the audit log, because the audit
    log deliberately holds no per-image detail."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from stylist_worker.stages.moderate import moderate_stage

    user_id, job_id, garment_id = await seed(owner_engine)
    ctx = _ctx(user_id, job_id, garment_id, {"store": FakeStore(), "ml": FakeML("pass", 0.03)})
    try:
        await moderate_stage.run(ctx)
        maker = async_sessionmaker(owner_engine, expire_on_commit=False)
        async with maker() as session:
            row = (
                (
                    await session.execute(
                        text("SELECT moderation, needs_review FROM garments WHERE id = :g"),
                        {"g": garment_id},
                    )
                )
                .mappings()
                .one()
            )
        assert row["moderation"]["verdict"] == "pass"
        assert row["moderation"]["model"] == "vit_nsfw_detector"
        assert row["needs_review"] is False
    finally:
        await cleanup(owner_engine, user_id)


def test_moderate_runs_before_every_stage_that_can_export_pixels() -> None:
    """The gate's POSITION is the policy. Asserted against the real pipeline
    order so a future reshuffle cannot quietly move a provider call in front of
    it."""
    from stylist_worker.stages import INGEST_STAGES

    names = [s.name for s in INGEST_STAGES]
    moderate_at = names.index("moderate")
    for exporting in ("tag",):
        assert names.index(exporting) > moderate_at, (
            f"{exporting} can export pixels and must run AFTER moderate"
        )
    # And after the local stages that read the file, so it sees the sanitised
    # image rather than raw EXIF-bearing bytes.
    assert moderate_at > names.index("sanitise")
