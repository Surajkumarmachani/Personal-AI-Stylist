"""Phase 9: asynchronous data export (§C5 portability)

Revision ID: 0013_export
Revises: 0012_erasure
Create Date: 2026-09-15

WHY THIS IS A JOB AND NOT A RESPONSE
-------------------------------------
The first version built the ZIP in memory and returned it from the request.
That is fine for nine garments and impossible for two thousand: the archive is
every cutout the user owns, so it grows with the wardrobe while the request
timeout does not. Building it in a worker and handing back a link is the only
shape that does not have a size at which it silently starts failing.

AN EXPORT IS A SECOND COPY OF EVERYTHING, SO IT EXPIRES
---------------------------------------------------------
This table exists mostly to make that true. A completed export is a full,
downloadable duplicate of a user's wardrobe sitting in object storage — the
exact thing the erasure saga works to remove. §C5 says "7-day link"; the link
expiring is not enough, so `expires_at` drives an actual delete of the object.

An export that outlives its purpose is a liability that grows with every user
who ever clicked the button.

ONE AT A TIME PER USER
----------------------
A partial unique index, not a handler check. Building the archive reads every
row and every image the user owns; letting someone queue fifty is a cost and a
data-duplication problem, and "the second click did nothing" is the behaviour
people expect from a button that is already working.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_export"
down_revision = "0012_erasure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "export_request",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # pending -> building -> ready | failed
        sa.Column("state", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("object_key", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        # What went in, so a user can tell an empty export from a broken one.
        sa.Column(
            "manifest", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        # §C5's "7-day link". Stored so the SWEEP can find it: a presigned URL
        # expiring only stops new downloads, it does not remove the copy.
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now() + interval '7 days'"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    # One in flight per user. Enforced here rather than in the handler because a
    # double-tap on a slow connection is two concurrent requests, and a handler
    # check races itself.
    op.create_index(
        "uq_export_in_flight",
        "export_request",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('pending', 'building')"),
    )
    # The expiry sweep's query.
    op.create_index(
        "ix_export_expiry",
        "export_request",
        ["expires_at"],
        postgresql_where=sa.text("deleted_at IS NULL AND object_key IS NOT NULL"),
    )

    op.execute("ALTER TABLE export_request ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE export_request FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON export_request
          USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
          WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON export_request TO stylist_app")

    # The expiry sweep runs with no tenant context — it is cleaning up after
    # everyone — so it needs the same SECURITY DEFINER treatment every other
    # cross-tenant driver here needs. Without it a FORCE-RLS select returns
    # zero rows with no error and the sweep reports "nothing to clean" forever
    # while the copies pile up.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION expired_exports()
        RETURNS TABLE (id uuid, user_id uuid, object_key text)
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT e.id, e.user_id, e.object_key
            FROM export_request e
            WHERE e.deleted_at IS NULL
              AND e.object_key IS NOT NULL
              AND e.expires_at < now()
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION expired_exports() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION expired_exports() TO stylist_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS expired_exports()")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON export_request")
    op.drop_index("ix_export_expiry", table_name="export_request")
    op.drop_index("uq_export_in_flight", table_name="export_request")
    op.drop_table("export_request")
