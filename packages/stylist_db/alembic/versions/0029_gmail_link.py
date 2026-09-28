"""Connect Gmail: read store order emails straight from the user's inbox.

The second way into Phase 15, beside forwarding. The Google grant lives in
`calendar_link` as provider 'google_gmail', beside the Calendar link, so the
existing export redaction and erasure revocation cover it with no new table.

The sync cron runs with no tenant context and `calendar_link` is FORCE RLS, so
it lists linked tenants through one SECURITY DEFINER function, the same shape as
push_tenants(): user ids only, no tokens, nothing else about anyone.
"""

from __future__ import annotations

from alembic import op

revision = "0029_gmail_link"
down_revision = "0028_purchase_email"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION gmail_tenants()
        RETURNS TABLE (user_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT l.user_id FROM calendar_link l
            WHERE l.provider = 'google_gmail'
              AND l.refresh_token IS NOT NULL
              AND l.revoked_at IS NULL
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION gmail_tenants() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION gmail_tenants() TO stylist_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS gmail_tenants()")
