"""Configuration. Everything from the environment, nothing hardcoded.

Note what is NOT here: model names and versions. Those live in Postgres
(system_config) and are served per-job, so swapping a model is an UPDATE rather
than a deploy (§D1). Reading a model name from an env var is how you end up
unable to roll back without a redeploy.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = Field(default="local")
    log_level: str = Field(default="INFO")

    # ---- database ----
    # This DSN must point at the least-privilege app role (NOBYPASSRLS), never
    # the owner/superuser — RLS is bypassed by superusers, so connecting as one
    # silently disables every tenant policy.
    database_url: str = Field(
        default="postgresql+asyncpg://stylist_app:stylist_app_local_only@localhost:5432/stylist"
    )
    # PROVISIONAL: retune in P9. The RATIO (api 20 > workers 10 > ml 5) is a
    # deliberate bulkhead so a worker burst cannot exhaust Postgres and take
    # down the API (§C2). The absolute numbers are invented against imagined
    # traffic and mean nothing until measured in Phases 5-8.
    db_pool_size: int = Field(default=20)

    # ---- redis: two logical dbs, never one ----
    redis_queue_url: str = Field(default="redis://localhost:6379/0")
    redis_cache_url: str = Field(default="redis://localhost:6379/1")

    # ---- object storage ----
    s3_bucket: str = Field(default="stylist-local")
    s3_endpoint_url: str | None = Field(default="http://localhost:9000")
    # Host baked into presigned URLs handed to clients. Unset in production
    # (S3/R2 is public DNS); set in compose, where the API reaches storage at
    # http://minio:9000 but the browser must use http://localhost:9000.
    s3_public_endpoint_url: str | None = Field(default=None)
    s3_region: str = Field(default="us-east-1")
    s3_access_key: str | None = Field(default="minioadmin")
    s3_secret_key: str | None = Field(default="minioadmin")
    presign_ttl_seconds: int = Field(default=900)

    # ---- internal services ----
    # The worker calls the ml service over HTTP rather than importing the
    # models: they scale on different axes (§View 2), and ml holds no DB or
    # storage credentials.
    # Browser origins allowed to call this API. An EXPLICIT allowlist, not "*":
    # the API is credential-bearing (Bearer tokens in Authorization), and a
    # wildcard on a credentialed API is what turns any page the user visits
    # into a client of their wardrobe. Comma-separated so one env var covers
    # local dev plus a preview deploy.
    cors_allow_origins: str = Field(default="http://localhost:3100")

    ml_base_url: str = Field(default="http://localhost:8081")

    # ---- LiteLLM gateway ----
    # The ONLY egress path to model providers. Every model call goes through
    # it, including self-hosted embeddings priced at zero — that pricing is
    # what makes budget exhaustion a downgrade rather than an outage (§B3).
    litellm_base_url: str = Field(default="http://localhost:4000")
    litellm_master_key: str = Field(default="sk-master-local-only")
    # `vlm-tagger-mock` locally: exercises the whole gateway path with no
    # credentials, which matters because Phase 0.3's DPA is outstanding.
    vlm_model: str = Field(default="vlm-tagger-mock")
    # Free-tier monthly LLM+VLM budget per tenant (§B3). Enforced by LiteLLM as
    # a hard budget on the virtual key, not by feature code.
    free_tier_monthly_budget_usd: float = Field(default=0.15)

    # ---- SSE progress stream ----
    # PROVISIONAL: retune in P9. 500ms is invisible inside a 10s ingest budget;
    # the 300s cap stops a phone that backgrounds mid-upload from pinning a
    # connection (and its DB session) indefinitely. Configurable mainly so
    # tests can shrink the cap — a test that waits out the production cap makes
    # the suite unusable.
    sse_poll_interval_seconds: float = Field(default=0.5)
    sse_max_stream_seconds: float = Field(default=300.0)

    # ---- auth ----
    jwt_secret: str = Field(default="dev-only-do-not-use-in-any-real-environment")
    jwt_algorithm: str = Field(default="HS256")
    access_token_minutes: int = Field(default=15)
    refresh_token_days: int = Field(default=30)

    @field_validator("jwt_secret")
    @classmethod
    def _reject_default_secret_outside_local(cls, v: str, info: ValidationInfo) -> str:
        env = (info.data or {}).get("environment", "local")
        if env != "local" and v.startswith("dev-only"):
            raise ValueError("jwt_secret must be set outside the local environment")
        return v

    @property
    def db_dsn_sync(self) -> str:
        return self.database_url.replace("+asyncpg", "")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
