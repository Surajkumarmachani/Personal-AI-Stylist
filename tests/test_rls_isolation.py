"""Cross-tenant isolation. REQUIRED CI CHECK — never skippable, never xfail.

This file is the reason RLS exists rather than trusting the ORM. Every test
here runs as the least-privilege app role, because a superuser bypasses RLS and
would make the whole suite a no-op that passes.

The load-bearing test is `test_query_without_where_clause_is_still_scoped`:
application code WILL forget a `.where(user_id == ...)` eventually, and that
must not be a data leak.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.asyncio


async def _as_tenant(session, tenant: uuid.UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(tenant)}
    )


# --------------------------------------------------------------------------
# Guard tests: if these fail, every other test in this file is meaningless.
# --------------------------------------------------------------------------


async def test_app_role_cannot_bypass_rls(app_sessionmaker) -> None:
    """The app role must be NOSUPERUSER and NOBYPASSRLS.

    Either privilege silently disables every tenant policy, so this is checked
    explicitly rather than assumed from the migration.
    """
    async with app_sessionmaker() as session:
        row = await session.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        )
        is_super, can_bypass = row.one()
    assert is_super is False, "app role is a superuser — RLS is not being enforced"
    assert can_bypass is False, "app role has BYPASSRLS — RLS is not being enforced"


async def test_rls_enabled_and_forced_on_every_tenant_table(app_sessionmaker) -> None:
    """Adding a user-scoped table without a policy must fail CI, not leak."""
    from stylist_db.models import TENANT_SCOPED_TABLES

    async with app_sessionmaker() as session:
        rows = await session.execute(
            text(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                       (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid)
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = ANY(:names)
                """
            ),
            {"names": list(TENANT_SCOPED_TABLES)},
        )
        found = {r[0]: (r[1], r[2], r[3]) for r in rows}

    missing = set(TENANT_SCOPED_TABLES) - set(found)
    assert not missing, f"tenant-scoped tables absent from the database: {missing}"
    for table, (enabled, forced, policy_count) in found.items():
        assert enabled, f"{table}: RLS is not enabled"
        assert forced, f"{table}: RLS is not FORCEd (the owner would bypass it)"
        assert policy_count >= 1, f"{table}: no policy attached"


# --------------------------------------------------------------------------
# The isolation properties themselves.
# --------------------------------------------------------------------------


async def test_no_cross_tenant_read(app_sessionmaker, two_tenants) -> None:
    tenant_a, tenant_b = two_tenants
    async with app_sessionmaker() as session, session.begin():
        await _as_tenant(session, tenant_b)
        rows = await session.execute(text("SELECT user_id FROM garments"))
        owners = [r[0] for r in rows]

    assert len(owners) == 2, f"tenant B should see its own 2 garments, saw {len(owners)}"
    assert set(owners) == {tenant_b}, "tenant B saw another tenant's rows"
    assert tenant_a not in owners


async def test_query_without_where_clause_is_still_scoped(app_sessionmaker, two_tenants) -> None:
    """THE POINT OF RLS. A SELECT with no filter at all must return only the
    caller's rows. This is the test to write first and never delete."""
    tenant_a, _tenant_b = two_tenants
    async with app_sessionmaker() as session, session.begin():
        await _as_tenant(session, tenant_a)
        total = await session.execute(text("SELECT count(*) FROM garments"))
        count = total.scalar_one()

    # 5 rows exist across both tenants; tenant A owns 3.
    assert count == 3, f"unfiltered count leaked across tenants: got {count}, expected 3"


async def test_no_cross_tenant_update(app_sessionmaker, two_tenants) -> None:
    """An UPDATE targeting another tenant's row affects zero rows — silently,
    which is correct. It must not raise, and it must not succeed."""
    tenant_a, tenant_b = two_tenants
    async with app_sessionmaker() as session, session.begin():
        await _as_tenant(session, tenant_a)
        row = await session.execute(text("SELECT id FROM garments LIMIT 1"))
        a_garment_id = row.scalar_one()

    async with app_sessionmaker() as session, session.begin():
        await _as_tenant(session, tenant_b)
        result = await session.execute(
            text("UPDATE garments SET needs_review = true WHERE id = :id"),
            {"id": a_garment_id},
        )
        assert result.rowcount == 0, "tenant B updated tenant A's garment"


async def test_no_cross_tenant_delete(app_sessionmaker, two_tenants) -> None:
    tenant_a, tenant_b = two_tenants
    async with app_sessionmaker() as session, session.begin():
        await _as_tenant(session, tenant_a)
        row = await session.execute(text("SELECT id FROM garments LIMIT 1"))
        a_garment_id = row.scalar_one()

    async with app_sessionmaker() as session, session.begin():
        await _as_tenant(session, tenant_b)
        result = await session.execute(
            text("DELETE FROM garments WHERE id = :id"), {"id": a_garment_id}
        )
        assert result.rowcount == 0, "tenant B deleted tenant A's garment"

    # And it is genuinely still there.
    async with app_sessionmaker() as session, session.begin():
        await _as_tenant(session, tenant_a)
        still = await session.execute(
            text("SELECT count(*) FROM garments WHERE id = :id"), {"id": a_garment_id}
        )
        assert still.scalar_one() == 1


async def test_cannot_insert_row_stamped_with_another_tenant(app_sessionmaker, two_tenants) -> None:
    """The write side of isolation: B cannot stamp a row with A's user_id.

    Postgres enforces this from WITH CHECK, falling back to the USING
    expression when WITH CHECK is absent — so this passes under either policy
    shape. The test earns its place regardless: it pins the *behaviour*, so a
    future policy edit that narrows the write rule (or a FOR SELECT-only
    policy, which has no write check at all) fails here instead of in
    production.
    """
    tenant_a, tenant_b = two_tenants
    from sqlalchemy.exc import ProgrammingError

    async with app_sessionmaker() as session:
        with pytest.raises(ProgrammingError) as exc_info:
            async with session.begin():
                await _as_tenant(session, tenant_b)
                await session.execute(
                    text(
                        "INSERT INTO garments (id, user_id, original_key, state) "
                        "VALUES (:id, :uid, 'x', 'received')"
                    ),
                    {"id": uuid.uuid4(), "uid": tenant_a},
                )
    assert "row-level security" in str(exc_info.value).lower()


async def test_no_tenant_context_returns_zero_rows(app_sessionmaker, two_tenants) -> None:
    """Fail-closed. With app.user_id unset the policy compares against NULL, so
    nothing matches — and critically it does NOT raise 22P02 on an empty
    string, which is why the policy wraps current_setting in NULLIF."""
    async with app_sessionmaker() as session, session.begin():
        rows = await session.execute(text("SELECT count(*) FROM garments"))
        assert rows.scalar_one() == 0

        await session.execute(text("SELECT set_config('app.user_id', '', true)"))
        rows = await session.execute(text("SELECT count(*) FROM garments"))
        assert rows.scalar_one() == 0


async def test_tenant_setting_does_not_leak_across_transactions(
    app_sessionmaker, two_tenants
) -> None:
    """set_config(..., true) is transaction-local, so a pooled connection cannot
    carry one request's identity into the next. With session-scoped SET plus
    PgBouncer, it would — that is the cross-tenant bug this guards."""
    tenant_a, _ = two_tenants
    async with app_sessionmaker() as session:
        async with session.begin():
            await _as_tenant(session, tenant_a)
            rows = await session.execute(text("SELECT count(*) FROM garments"))
            assert rows.scalar_one() == 3

        # New transaction, same connection, no tenant set.
        async with session.begin():
            rows = await session.execute(text("SELECT count(*) FROM garments"))
            assert rows.scalar_one() == 0, "tenant context survived the transaction"
