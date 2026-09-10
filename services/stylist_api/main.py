"""FastAPI application factory.

The OpenAPI spec generated here is the contract clients are tested against
(§D2), so response models are declared on every route rather than inferred.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from stylist_api.routers import auth, garments, health, jobs
from stylist_api.settings import get_settings
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
    app = FastAPI(
        title="Personal AI Stylist API",
        version="0.1.0",
        summary="Wardrobe cataloguing and outfit suggestions",
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(garments.router)
    app.include_router(jobs.router)

    from stylist_api.routers import uploads

    app.include_router(uploads.router)
    return app


app = create_app()
