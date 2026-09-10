"""FastAPI application factory.

The OpenAPI spec generated here is the contract clients are tested against
(§D2), so response models are declared on every route rather than inferred.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from stylist_api.routers import auth, corrections, garments, health, jobs
from stylist_api.settings import get_settings
from stylist_clients.litellm_client import LiteLLMClient
from stylist_clients.redis_client import CacheRedis, QueueRedis
from stylist_clients.storage import ObjectStore
from stylist_db.session import dispose_engine, init_engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    init_engine(settings.database_url, pool_size=settings.db_pool_size)
    app.state.queue_redis = QueueRedis(settings.redis_queue_url)
    app.state.cache_redis = CacheRedis(settings.redis_cache_url)
    app.state.litellm = LiteLLMClient(settings.litellm_base_url, settings.litellm_master_key)
    app.state.object_store = ObjectStore(
        bucket=settings.s3_bucket,
        endpoint_url=settings.s3_endpoint_url,
        region=settings.s3_region,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        presign_ttl_seconds=settings.presign_ttl_seconds,
        public_endpoint_url=settings.s3_public_endpoint_url,
    )
    try:
        yield
    finally:
        await app.state.queue_redis.close()
        await app.state.cache_redis.close()
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Personal AI Stylist API",
        version="0.1.0",
        summary="Wardrobe cataloguing and outfit suggestions",
        lifespan=lifespan,
    )
    # CORS. Without this the web UI cannot call the API from a browser at all:
    # the preflight OPTIONS returns 405 with no Access-Control-* headers, the
    # browser blocks the request, and the UI shows only
    # "TypeError: Failed to fetch" — which names neither the cause nor the
    # service. Nothing caught it earlier because every test drives the API
    # server-side with httpx, and CORS is enforced by browsers, not servers.
    origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        # False, deliberately. Auth is a Bearer token in a header, not a
        # cookie, so credentialed CORS buys nothing — and turning it on would
        # forbid the wildcard some deploy is bound to reach for later.
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        # Idempotency-Key is required on ingest, so it MUST be allowed here or
        # every upload preflight fails while auth appears to work.
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(garments.router)
    app.include_router(jobs.router)
    app.include_router(corrections.router)

    from stylist_api.routers import uploads

    app.include_router(uploads.router)
    return app


app = create_app()
