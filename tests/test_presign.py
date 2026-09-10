"""Presigned upload policy.

These assert the SECURITY PROPERTY, not just the happy path: the size ceiling
and content-type restriction must be conditions inside the signed policy
document, because the client uploads straight to storage and our API is not in
the byte path to check anything.

No live MinIO needed — presigning is pure signing, so the policy can be decoded
and inspected offline. Whether the object actually lands is a compose-level
check, asserted separately.
"""

from __future__ import annotations

import base64
import json
import uuid

import pytest

from stylist_clients.storage import (
    ALLOWED_CONTENT_TYPES,
    MAX_UPLOAD_BYTES,
    MIN_UPLOAD_BYTES,
    ObjectStore,
)


@pytest.fixture
def store() -> ObjectStore:
    return ObjectStore(
        bucket="stylist-test",
        endpoint_url="http://localhost:9000",
        access_key="test",
        secret_key="test",
    )


def _decoded_policy(fields: dict[str, str]) -> dict:
    return json.loads(base64.b64decode(fields["policy"]))


def test_size_ceiling_is_in_the_signed_policy(store: ObjectStore) -> None:
    """The whole reason for presigned POST over PUT. Without this condition the
    client could upload an arbitrarily large object and nothing would stop it."""
    presigned = store.presign_upload(user_id=uuid.uuid4(), content_type="image/jpeg")
    conditions = _decoded_policy(presigned.fields)["conditions"]

    ranges = [c for c in conditions if isinstance(c, list) and c[0] == "content-length-range"]
    assert ranges, f"no content-length-range condition in policy: {conditions}"
    _, lower, upper = ranges[0]
    assert upper == MAX_UPLOAD_BYTES
    assert lower == MIN_UPLOAD_BYTES


def test_content_type_is_pinned_in_the_signed_policy(store: ObjectStore) -> None:
    presigned = store.presign_upload(user_id=uuid.uuid4(), content_type="image/png")
    conditions = _decoded_policy(presigned.fields)["conditions"]
    assert any(isinstance(c, dict) and c.get("Content-Type") == "image/png" for c in conditions), (
        f"Content-Type not constrained in policy: {conditions}"
    )


def test_key_is_tenant_prefixed(store: ObjectStore) -> None:
    """Erasure step 4 deletes a user's objects by prefix, so the tenant id has
    to be in the key — otherwise deletion becomes a full scan (§C5)."""
    user_id = uuid.uuid4()
    presigned = store.presign_upload(user_id=user_id, content_type="image/jpeg")
    assert presigned.key.startswith(f"originals/{user_id}/")


@pytest.mark.parametrize(
    "content_type",
    ["application/pdf", "text/html", "image/svg+xml", "image/gif", "application/octet-stream"],
)
def test_disallowed_content_types_are_rejected(store: ObjectStore, content_type: str) -> None:
    # image/svg+xml is on this list deliberately: SVG is a script-execution
    # vector, not a photo format.
    with pytest.raises(ValueError):
        store.presign_upload(user_id=uuid.uuid4(), content_type=content_type)


def test_client_cannot_request_a_larger_ceiling(store: ObjectStore) -> None:
    with pytest.raises(ValueError):
        store.presign_upload(
            user_id=uuid.uuid4(),
            content_type="image/jpeg",
            max_bytes=MAX_UPLOAD_BYTES * 10,
        )


def test_allowlist_covers_the_formats_phones_actually_produce() -> None:
    # HEIC/HEIF is the iPhone default. Omitting it means most iOS uploads fail
    # at the policy with an error the user cannot act on.
    assert "image/heic" in ALLOWED_CONTENT_TYPES
    assert "image/jpeg" in ALLOWED_CONTENT_TYPES


def test_presign_expiry_is_short(store: ObjectStore) -> None:
    """View 1: signed URLs only, <= 15 min. A long-lived upload URL is a
    credential with no revocation path."""
    assert store.presign_ttl_seconds <= 900
