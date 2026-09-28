"""Issue a fresh LiteLLM virtual key for tenants whose key this gateway lacks.

Signup creates one key per tenant (routers/auth.py), and a gateway outage at
that moment leaves the column NULL; the signup code calls the key
"backfillable", and this is the backfill. It is also what makes moving an
account between environments work: a key issued by one LiteLLM means nothing
to another, so a migrated tenant would otherwise have every tagging and
rerank call rejected.

    python scripts/reissue_litellm_keys.py                  # NULL keys only
    python scripts/reissue_litellm_keys.py --email a@b.com  # force one tenant

Runs as the app role: tenants are listed from `users`, and each profile is
updated inside that tenant's own RLS session.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import text

from stylist_api.settings import get_settings
from stylist_clients.litellm_client import LiteLLMClient
from stylist_db.session import init_engine, system_session, tenant_session


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", help="reissue for this account even if it has a key")
    args = ap.parse_args()

    s = get_settings()
    init_engine(s.database_url)
    gateway = LiteLLMClient(s.litellm_base_url, s.litellm_master_key)

    # `users` is readable without tenant context (login needs it);
    # `user_profile` is FORCE RLS, so each profile is read and written inside
    # that tenant's own session. system_session would see zero profiles.
    async with system_session() as session:
        if args.email:
            rows = await session.execute(
                text("SELECT id FROM users WHERE lower(email) = lower(:e)"), {"e": args.email}
            )
        else:
            rows = await session.execute(text("SELECT id FROM users"))
        user_ids = [r[0] for r in rows]
    if args.email and not user_ids:
        print(f"no account {args.email}", file=sys.stderr)
        return 1

    issued_count = 0
    for uid in user_ids:
        async with tenant_session(uid) as session:
            current = (
                await session.execute(text("SELECT litellm_key FROM user_profile LIMIT 1"))
            ).scalar()
            if current and not args.email:
                continue
            issued = await gateway.create_virtual_key(
                user_id=uid, max_budget=s.free_tier_monthly_budget_usd
            )
            await session.execute(
                text("UPDATE user_profile SET litellm_key = :k"), {"k": issued.key}
            )
            issued_count += 1
            print(f"issued a key for {uid}")
    print(f"done: {issued_count} key(s) issued")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
