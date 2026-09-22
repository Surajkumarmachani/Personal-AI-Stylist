"""Grant or revoke admin on an account.

WHY THIS IS A SCRIPT AND NOT AN ENDPOINT
----------------------------------------
`is_admin` unlocks `/ops`, which serves CROSS-TENANT aggregates — the whole
deployment's ingest funnel, latency, DLQ depth and model spend. An API that
can grant that to its own callers is a privilege-escalation path wearing an
admin panel: compromise one account, call one endpoint, read everyone's
operational data.

Setting it needs database access, which is the same bar as reading what it
unlocks. That is the point, not an inconvenience to be smoothed over later.

    python scripts/grant_admin.py --email you@example.com
    python scripts/grant_admin.py --email you@example.com --revoke
    python scripts/grant_admin.py --list
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import text

from stylist_db.session import init_engine, system_session


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email")
    ap.add_argument("--revoke", action="store_true")
    ap.add_argument("--list", action="store_true", help="show who currently has it")
    args = ap.parse_args()

    init_engine(os.environ["DATABASE_URL"])

    # system_session, not tenant_session: this is an operator action about
    # another account, so there is no tenant context to scope it to.
    async with system_session() as s:
        if args.list or not args.email:
            rows = await s.execute(
                text("SELECT email FROM users WHERE is_admin AND deleted_at IS NULL ORDER BY email")
            )
            admins = [r[0] for r in rows]
            print(f"  {len(admins)} admin(s):" if admins else "  no admins yet")
            for a in admins:
                print(f"    {a}")
            if not args.email:
                return 0

        result = await s.execute(
            text(
                "UPDATE users SET is_admin = :grant, updated_at = now() "
                "WHERE email = :email AND deleted_at IS NULL RETURNING email, is_admin"
            ),
            {"grant": not args.revoke, "email": args.email},
        )
        row = result.first()

    if row is None:
        print(f"  no such account: {args.email}")
        return 1
    print(f"  {row[0]} is_admin = {row[1]}")
    return 0


sys.exit(asyncio.run(main()))
