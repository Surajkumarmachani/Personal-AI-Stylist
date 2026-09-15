"""Phase 8: Google Calendar link (refresh token + consent record)

Revision ID: 0010_calendar_link
Revises: 0009_feedback_style
Create Date: 2026-09-15

A SEPARATE TABLE, NOT A COLUMN ON user_profile
-----------------------------------------------
It would be one column. It is a table because of what deleting it has to mean.

A calendar link is CONSENT to read something outside this product, and §C5's
erasure saga treats third-party grants as their own step — revoke at the
provider, then forget locally, and record that both happened. A nullable column
on `user_profile` makes "revoked" and "never connected" the same value (NULL),
so nothing can answer "did we successfully revoke this, or did the call fail
and we cleared it anyway?" That question has a regulator behind it.

The same reasoning is why `revoked_at` exists alongside deletion: the row is
kept, tokenless, as the record that a revocation happened and when.

THE TOKEN IS THE SENSITIVE PART, AND THIS IS NOT ENCRYPTION AT REST
--------------------------------------------------------------------
`refresh_token` is stored as text under RLS, exactly like `litellm_key` on
`user_profile`. That is the same posture the rest of this project already has
and it is NOT good enough for production: a refresh token reads a user's
calendar until revoked. Encrypting it needs a KMS and a key-rotation story,
which is Phase 9 infrastructure work. Recorded here as a KNOWN GAP rather than
left to be discovered.

READ-ONLY SCOPE, RECORDED
-------------------------
`scope` is stored because the grant can be narrower than we asked for — Google
lets the user uncheck individual scopes on the consent screen. Assuming we got
what we requested is how you get a 403 on a sync and no idea why.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_calendar_link"
down_revision = "0009_feedback_style"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calendar_link",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(16), nullable=False, server_default=sa.text("'google'")),
        # NULL once revoked. The ROW survives as the consent record; see above.
        sa.Column(
            "refresh_token",
            sa.Text(),
            nullable=True,
            comment="KNOWN GAP: plaintext under RLS, like litellm_key. "
            "Needs KMS envelope encryption before production (P9).",
        ),
        # What Google actually granted, which can be narrower than we asked.
        sa.Column("scope", sa.Text(), nullable=True),
        # The account the grant belongs to. Shown in the UI so a user with two
        # Google accounts can see WHICH one is linked — "connected" with no
        # address is unactionable when the wrong calendar is syncing.
        sa.Column("account_email", sa.String(320), nullable=True),
        sa.Column(
            "connected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        # Whether the revoke call to Google SUCCEEDED. False after a local
        # clear that the provider did not confirm, which is a materially
        # different state to disclose under an erasure request.
        sa.Column(
            "revoked_at_provider", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.create_index(
        "uq_calendar_link_provider", "calendar_link", ["user_id", "provider"], unique=True
    )

    op.execute("ALTER TABLE calendar_link ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE calendar_link FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON calendar_link
          USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
          WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON calendar_link TO stylist_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON calendar_link")
    op.drop_index("uq_calendar_link_provider", table_name="calendar_link")
    op.drop_table("calendar_link")
