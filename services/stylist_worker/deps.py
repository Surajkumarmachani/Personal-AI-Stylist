"""Process-wide clients for the worker.

Built once at startup and reused: a boto3 client creation costs ~100ms of
session/credential resolution, and doing that per photo across a 60-photo
onboarding burst is 6 seconds of pure waste.

Stages reach these through `ctx.scratch` when a test injects a fake, and fall
back to these singletons otherwise — which is what keeps the stages testable
without a live S3 or model service while leaving production wiring trivial.
"""

from __future__ import annotations

from stylist_api.settings import get_settings
from stylist_clients.ml_client import MLClient
from stylist_clients.storage import ObjectStore

_store: ObjectStore | None = None
_ml: MLClient | None = None


def get_object_store() -> ObjectStore:
    global _store
    if _store is None:
        s = get_settings()
        _store = ObjectStore(
            bucket=s.s3_bucket,
            endpoint_url=s.s3_endpoint_url,
            region=s.s3_region,
            access_key=s.s3_access_key,
            secret_key=s.s3_secret_key,
            presign_ttl_seconds=s.presign_ttl_seconds,
            public_endpoint_url=s.s3_public_endpoint_url,
        )
    return _store


def get_ml_client() -> MLClient:
    global _ml
    if _ml is None:
        _ml = MLClient(get_settings().ml_base_url)
    return _ml


def reset() -> None:
    """For tests, so a fake does not leak between cases."""
    global _store, _ml
    _store = None
    _ml = None
