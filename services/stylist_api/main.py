"""FastAPI application factory.

The OpenAPI spec generated here is the contract clients are tested against
(§D2), so response models are declared on every route rather than inferred.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError

from stylist_api.middleware import RequestOutcomeMiddleware
from stylist_api.routers import (
    auth,
    boards,
    calendar,
    chat,
    corrections,
    duplicates,
    evalview,
    feedback,
    garments,
    health,
    jobs,
    location,
    occasions,
    ops,
    privacy,
    push,
    search,
    suggestions,
    tryon,
    wear,
)
from stylist_api.settings import get_settings
from stylist_clients.litellm_client import LiteLLMClient
from stylist_clients.redis_client import CacheRedis, QueueRedis
from stylist_clients.storage import ObjectStore
from stylist_db.session import dispose_engine, init_engine
from stylist_obs import configure_tracing

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_tracing("stylist-api")
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

    # A DEPENDENCY BEING DOWN IS A 503, NOT A 500.
    #
    # Measured during the 2026-09-15 game day: with Postgres stopped, every
    # endpoint returned 500 — `/suggestions`, `/auth/login`, all of it. That is
    # the wrong instruction to everyone who reads it. A 500 says "we have a
    # bug": do not retry, page someone, look at a traceback. A 503 says "the
    # thing we depend on is down": retry with backoff, and the traceback will
    # not help you.
    #
    # It also made `api_5xx` unable to distinguish a database outage from a bad
    # deploy, which is the first question the runbook for that alert asks.
    #
    # Registered for the DRIVER-level errors only. An `OperationalError` from
    # SQLAlchemy is genuinely "cannot reach the database"; a `ProgrammingError`
    # is our SQL being wrong and must stay a 500, because turning a bug into a
    # retryable status is how a broken query becomes an infinite retry loop.
    #
    # `socket.gaierror` and `ConnectionError` are here because the SQLAlchemy
    # types ALONE DID NOT CATCH IT. Retested with Postgres stopped: the
    # exception that reached the handler was a raw
    # `socket.gaierror: [Errno -2] Name or service not known` — DNS failing
    # before a connection exists, so there is nothing for SQLAlchemy to wrap.
    # Registering only the ORM exceptions looked correct, passed a structural
    # test, and still returned 500 to every request.
    #
    # Both are `OSError` subclasses, but `OSError` itself is NOT registered:
    # that would turn a missing file or a full disk into "retry shortly".
    @app.exception_handler(OperationalError)
    @app.exception_handler(InterfaceError)
    @app.exception_handler(socket.gaierror)
    @app.exception_handler(ConnectionError)
    async def _dependency_unavailable(request: Request, exc: Exception) -> JSONResponse:
        logger.error("dependency unavailable on %s: %s", request.url.path, type(exc).__name__)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "a required service is temporarily unavailable; retry shortly"},
            # Tells a well-behaved client how long to wait instead of making it
            # guess, and stops a retry storm arriving the instant we recover.
            headers={"Retry-After": "5"},
        )

    # Outermost: it must see the status code every other layer produces,
    # including the 500 Starlette synthesises from an unhandled exception, and
    # the 503 above.
    app.add_middleware(RequestOutcomeMiddleware)
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
    # Phase 5
    app.include_router(wear.router)
    app.include_router(search.router)
    app.include_router(duplicates.router)
    app.include_router(ops.router)
    app.include_router(evalview.router)
    # Phase 6
    app.include_router(suggestions.router)
    app.include_router(feedback.router)
    app.include_router(boards.router)
    app.include_router(calendar.router)
    app.include_router(push.router)
    app.include_router(privacy.router)
    app.include_router(tryon.router)
    app.include_router(chat.router)
    app.include_router(location.router)
    app.include_router(occasions.router)

    from stylist_api.routers import uploads

    app.include_router(uploads.router)
    return app


app = create_app()
