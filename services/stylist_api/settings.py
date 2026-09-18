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
    # PROVISIONAL — re-dated 2026-09-17: P9 arrived with no real traffic.
    # Resolves when: peak concurrent connections per service under real load.
    # The RATIO (api 20 > workers 10 > ml 5) is a
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
    # Phase 7's reranker. Points at the real row, which has NO mock twin — see
    # the long note in litellm/config.yaml. Without a provider key the call
    # fails and the suggestion degrades to deterministic order, so the degrade
    # ladder is the uncredentialed default; with a key it actually reranks.
    # A mock here would counterfeit `validator.reject{rule="unknown_id"}` on
    # every request, because a fixed response cannot echo per-request uuids.
    rerank_model: str = Field(default="outfit-reranker")
    # §C3 assertion 6. Below this the model's outfit is kept but ranked under
    # the deterministic order — an unsure model is not a lying model.
    rerank_min_confidence: float = Field(default=0.5)

    # ---- Google Calendar (Phase 8) ----
    # Unset by default, exactly like the provider keys: the calendar feature
    # degrades to "not connected" and every other path works untouched. A
    # missing credential must never be a startup failure.
    google_client_id: str = Field(default="")
    google_client_secret: str = Field(default="")
    # Must match a redirect URI registered on the OAuth client EXACTLY —
    # Google compares the string, so a trailing slash is a different URI and
    # produces `redirect_uri_mismatch` with no hint about which part differs.
    google_redirect_uri: str = Field(default="http://localhost:8080/calendar/callback")

    # ---- push notifications (Phase 8) ----
    # PATH to a Firebase service-account JSON, never the key itself. A
    # multi-line PEM inside an env var is mangled differently by every shell,
    # .env parser and CI secret store, and the usual "fix" — stripping the
    # newlines — produces an unparseable key and a stack trace three layers
    # down. Unset disables push; nothing else changes.
    firebase_credentials_file: str = Field(default="")

    # ---- try-on (Phase 10) ----
    # EMPTY, and that is the current correct value. Phase 10 opens with
    # ---- Virtual try-on ----
    # "Benchmark before you build" — a 10-body x 16-garment grid including
    # sarees, kurtas and a sherwani — because "the published benchmark used
    # Western garments; your routing table must come from your own grid".
    #
    # That grid gates the ROUTING TABLE (which model per category), not the
    # render path itself: there is no way to run the grid without a working
    # provider call, so exactly ONE provider is configurable here and there is
    # deliberately no fallback chain and no per-category selection. Those are
    # the parts that need the data. See stylist_clients.vton_client.
    #
    # One of: leffa | idm-vton | ootdiffusion. Unset degrades
    # `POST /outfits/{hash}/tryon` to the board, which is the exit criterion's
    # required behaviour anyway.
    vton_provider: str = Field(default="")
    # A Hugging Face token. NOT optional in practice: all three candidate
    # Spaces run on ZeroGPU, which rejects anonymous programmatic calls in
    # under a second with an empty error body. Without this, try-on degrades
    # to the board on every request and the reason is unobvious.
    vton_api_token: str = Field(default="")
    # Overrides the public Space URL for a self-hosted or dedicated Inference
    # Endpoint. The Gradio protocol is identical; only the host changes.
    vton_base_url: str = Field(default="")
    # Wall-clock ceiling for one render. 30-120s is normal on shared ZeroGPU
    # hardware and the queue is other people's traffic, so this is generous by
    # design — it exists to stop an abandoned stream holding a worker, not to
    # enforce a latency target we do not control.
    # PROVISIONAL — set 2026-09-17 with no measured render on this account.
    # Resolves when: p95 render latency over the benchmark grid.
    vton_timeout_s: float = Field(default=900.0)
    # Free-tier monthly LLM+VLM budget per tenant (§B3). Enforced by LiteLLM as
    # a hard budget on the virtual key, not by feature code.
    free_tier_monthly_budget_usd: float = Field(default=0.15)

    # ---- SSE progress stream ----
    # PROVISIONAL — re-dated 2026-09-17: P9 arrived with no real traffic.
    # Resolves when: measured SSE connection counts. 500ms is invisible
    # inside a 10s ingest budget;
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
