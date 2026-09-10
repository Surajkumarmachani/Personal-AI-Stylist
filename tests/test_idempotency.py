"""Idempotency-Key handling.

The property under test: POSTing /garments/ingest twice with the same
Idempotency-Key must return the SAME job id, not start a second pipeline over
the same photos. At 60-photo onboarding bursts on flaky mobile networks, retries
are normal traffic, and a duplicated ingest is a doubled VLM bill plus duplicate
wardrobe rows.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

from stylist_clients.redis_client import IDEMPOTENCY_TTL_SECONDS, QueueRedis

REDIS_URL = os.environ.get("REDIS_QUEUE_URL", "redis://localhost:6379/0")

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def redis():
    client = QueueRedis(REDIS_URL)
    try:
        await client.ping()
    except Exception:
        await client.close()
        pytest.skip(f"redis not reachable at {REDIS_URL}")
    yield client
    await client.close()


async def test_same_key_returns_the_original_job_id(redis: QueueRedis) -> None:
    user_id = uuid.uuid4()
    key = f"test-{uuid.uuid4()}"
    first_job, second_job = uuid.uuid4(), uuid.uuid4()

    first = await redis.claim_idempotency_key(user_id=user_id, key=key, job_ids=[first_job])
    assert first.created is True
    assert first.job_ids == (first_job,)

    # A retry arrives with a NEW candidate job id; it must be discarded in
    # favour of the original.
    second = await redis.claim_idempotency_key(user_id=user_id, key=key, job_ids=[second_job])
    assert second.created is False
    assert second.job_ids == (first_job,), "retry started a second job"


async def test_same_key_from_different_tenants_does_not_collide(redis: QueueRedis) -> None:
    """Two users can legitimately generate the same Idempotency-Key (a client
    library using a request counter, say). Their claims must be independent."""
    key = f"shared-{uuid.uuid4()}"
    a_job, b_job = uuid.uuid4(), uuid.uuid4()

    a = await redis.claim_idempotency_key(user_id=uuid.uuid4(), key=key, job_ids=[a_job])
    b = await redis.claim_idempotency_key(user_id=uuid.uuid4(), key=key, job_ids=[b_job])

    assert a.created is True
    assert b.created is True, "tenant B's claim was swallowed by tenant A's key"
    assert a.job_ids != b.job_ids


async def test_ttl_is_set_so_the_keyspace_cannot_grow_forever(redis: QueueRedis) -> None:
    user_id = uuid.uuid4()
    key = f"ttl-{uuid.uuid4()}"
    await redis.claim_idempotency_key(user_id=user_id, key=key, job_ids=[uuid.uuid4()])

    ttl = await redis._r.ttl(f"idem:{user_id}:{key}")
    assert 0 < ttl <= IDEMPOTENCY_TTL_SECONDS


async def test_revoked_refresh_token_is_detected(redis: QueueRedis) -> None:
    jti = str(uuid.uuid4())
    assert await redis.is_token_revoked(jti) is False
    await redis.revoke_token(jti, ttl_seconds=60)
    assert await redis.is_token_revoked(jti) is True


async def test_a_batch_replay_returns_every_job_not_just_the_first(redis: QueueRedis) -> None:
    """The bug this guards: a 10-photo batch replayed as 1 job id.

    An earlier version stored a single id per key and suffixed the rest, so a
    replay returned only the first job and the client silently lost track of
    the other nine — no error anywhere, just nine photos whose progress could
    never be watched.
    """
    user_id = uuid.uuid4()
    key = f"batch-{uuid.uuid4()}"
    batch = [uuid.uuid4() for _ in range(10)]

    first = await redis.claim_idempotency_key(user_id=user_id, key=key, job_ids=batch)
    assert first.created is True
    assert list(first.job_ids) == batch

    # The retry mints fresh candidate ids, as a real client would.
    replay = await redis.claim_idempotency_key(
        user_id=user_id, key=key, job_ids=[uuid.uuid4() for _ in range(10)]
    )
    assert replay.created is False
    assert len(replay.job_ids) == 10, f"replay returned {len(replay.job_ids)} of 10 jobs"
    assert list(replay.job_ids) == batch, "replay changed the ids or their order"
