"""Set the stylist_app role's password from APP_DB_PASSWORD.

Migration 0001 creates `stylist_app` with the password `stylist_app_local_only`
so a fresh local database works with no manual step. Outside local that
password is public (it is in git), so every deployed migrate run follows
`alembic upgrade head` with this script, connecting as the owner.

Idempotent: re-setting the same password is a no-op in effect, so it runs on
every deploy rather than only the first one — which is also what makes
rotation a matter of changing the secret and redeploying.
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg  # type: ignore[import-untyped]  # ships no stubs


async def main() -> int:
    dsn = os.environ.get("MIGRATION_DATABASE_URL", "")
    password = os.environ.get("APP_DB_PASSWORD", "")
    if not dsn or not password:
        print("MIGRATION_DATABASE_URL and APP_DB_PASSWORD are both required", file=sys.stderr)
        return 1
    if password == "stylist_app_local_only":
        print("APP_DB_PASSWORD is the public local default; refusing", file=sys.stderr)
        return 1

    # asyncpg wants a plain libpq-style DSN, not SQLAlchemy's driver suffix.
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        # ALTER ROLE cannot take a bind parameter, so quote the literal with
        # Postgres's own quoting rather than an f-string.
        literal = await conn.fetchval("SELECT quote_literal($1)", password)
        await conn.execute(f"ALTER ROLE stylist_app PASSWORD {literal}")
    finally:
        await conn.close()
    print("stylist_app password set")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
