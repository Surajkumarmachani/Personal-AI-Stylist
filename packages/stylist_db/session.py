"""Engine, sessions, and the tenant-scoping primitive that RLS depends on.

THE IMPORTANT DETAIL IN THIS FILE
---------------------------------
The plan's snippet was:

    await conn.execute(text("SET LOCAL app.user_id = :uid"), {"uid": user.id})

That does not work. Postgres `SET` does not accept bind parameters, so this
either errors or — worse — tempts you into f-stringing the uuid into DDL-ish
SQL, which is an injection sink on the one value that decides which tenant's
data you can see.

The correct parameterised form is `set_config(setting, value, is_local)`:

    SELECT set_config('app.user_id', :uid, true)

The third argument `true` means transaction-local, which is what `SET LOCAL`
gave us: the setting is discarded at COMMIT/ROLLBACK, so a pooled connection
cannot carry one request's tenant identity into the next. With `SET` (session
scope) plus PgBouncer, it would — that is a real and severe cross-tenant bug.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

TENANT_SETTING = "app.user_id"

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(dsn: str, *, pool_size: int = 20, echo: bool = False) -> AsyncEngine:
    """Create the process-wide engine.

    Pool sizes are per-service and deliberately different (api 20, workers 10,
    ml 5) so a worker burst cannot exhaust Postgres connections and take down
    the API — see the bulkhead table (§C2).

    # PROVISIONAL: retune in P9. The ratio is the design; the numbers are not
    # measured. Unlabelled invented numbers become load-bearing folklore.
    """
    global _engine, _sessionmaker
    _engine = create_async_engine(
        dsn,
        pool_size=pool_size,
        max_overflow=0,  # hard ceiling; overflow hides capacity problems
        pool_pre_ping=True,
        echo=echo,
    )
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("init_engine() has not been called")
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("init_engine() has not been called")
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def set_tenant(session: AsyncSession, user_id: uuid.UUID | str) -> None:
    """Bind the current transaction to one tenant. Must be inside a transaction.

    Parameterised via set_config — see the module docstring for why this is not
    `SET LOCAL`.
    """
    await session.execute(
        text(f"SELECT set_config('{TENANT_SETTING}', :uid, true)"),
        {"uid": str(user_id)},
    )


@asynccontextmanager
async def tenant_session(user_id: uuid.UUID | str) -> AsyncIterator[AsyncSession]:
    """A transaction scoped to one tenant. This is the ONLY way application code
    should touch tenant data.

    Every statement inside runs with app.user_id set, so the RLS policy filters
    rows even when a query forgets its WHERE clause — which is the entire point
    of using RLS instead of trusting the ORM.
    """
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await set_tenant(session, user_id)
        yield session


@asynccontextmanager
async def system_session() -> AsyncIterator[AsyncSession]:
    """A transaction with NO tenant context.

    For background work that legitimately spans tenants: the outbox relay, the
    nightly precompute driver, the erasure saga. Tenant-scoped tables return
    ZERO rows here, because the RLS policy compares against a NULL setting —
    that is fail-closed by design. If you need tenant rows, use tenant_session.
    """
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        yield session
