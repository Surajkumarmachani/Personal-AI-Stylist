"""Settings refuse local-only credentials once ENVIRONMENT is not `local`.

Pure construction tests: no database, no Redis. `_env_file=None` keeps a
developer's real .env out of the result, so these pass the same on a laptop
and in CI.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from stylist_api.settings import Settings

# A deployment's worth of real-looking values. Each test removes one.
DEPLOYED = {
    "environment": "production",
    "jwt_secret": "a" * 64,
    "database_url": "postgresql+asyncpg://stylist_app:s3cret@/stylist?host=/cloudsql/p:r:i",
    "s3_access_key": "GOOG1EXAMPLE",
    "s3_secret_key": "example-secret",
    "litellm_master_key": "sk-" + "b" * 64,
}


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **{**DEPLOYED, **overrides})  # type: ignore[arg-type]


def test_deployed_values_are_accepted() -> None:
    assert _settings().environment == "production"


def test_local_defaults_are_fine_locally() -> None:
    assert Settings(_env_file=None, environment="local").s3_access_key == "minioadmin"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("s3_access_key", "minioadmin", "MinIO defaults"),
        ("s3_secret_key", "minioadmin", "MinIO defaults"),
        ("litellm_master_key", "sk-master-local-only", "LITELLM_MASTER_KEY"),
        (
            "database_url",
            "postgresql+asyncpg://stylist_app:stylist_app_local_only@db:5432/stylist",
            "local-only app-role password",
        ),
        ("jwt_secret", "dev-only-do-not-use-in-any-real-environment", "jwt_secret"),
    ],
)
def test_local_credentials_are_rejected_outside_local(field: str, value: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _settings(**{field: value})
