"""VLM tagging: virtual keys, user corrections, moderation record

Revision ID: 0003_vlm_tagging
Revises: 0002_embeddings
Create Date: 2026-09-10

Three additions, each backing a Phase 4 guarantee:

`user_profile.litellm_key` — the tenant's virtual key. Stored per tenant
because the budget is enforced by the gateway ON THE CREDENTIAL: feature code
cannot exceed it even with a bug, and spend is attributable without joining
across the gateway's own schema.

`garments.user_verified_fields` already exists from 0001, and Phase 4 is where
it starts mattering. The tag stage checks it in SQL so a backfill can never
overwrite a field the user corrected — the rule that makes the correction UI
worth trusting.

`garment_corrections` — an append-only log of every field the user changed.
Not a mutation on the garment row: §D1 calls the correction rate "your live
accuracy metric — labelled data arriving free", and an UPDATE would destroy
exactly that. Storing before/after per field is what makes it possible to say
"material was wrong 40% of the time on silk" months later.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_vlm_tagging"
down_revision = "0002_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: tenants created before Phase 4 have no key, and the tag stage
    # degrades rather than failing for them. A NOT NULL here would break every
    # existing account's ingest at once.
    op.add_column("user_profile", sa.Column("litellm_key", sa.Text(), nullable=True))
    op.add_column(
        "user_profile",
        sa.Column(
            "litellm_budget_usd",
            sa.Numeric(10, 4),
            nullable=True,
            comment="Copy of the budget set on the gateway key, for display only. "
            "The gateway is the enforcement point; this is never checked.",
        ),
    )

    op.create_table(
        "garment_corrections",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "garment_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("garments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field_name", sa.String(64), nullable=False),
        # Text, not the field's own enum type: one table holds corrections for
        # every field, and a correction's value may no longer be legal if the
        # taxonomy changes. The log is history and must stay readable.
        sa.Column("old_value", sa.Text()),
        sa.Column("new_value", sa.Text()),
        sa.Column("extractor_version", sa.String(32)),
        # What the model claimed for this field when it got it wrong. Without
        # it, "confidently wrong" and "correctly uncertain" are
        # indistinguishable after the fact — and they need opposite fixes.
        sa.Column("model_confidence", sa.Numeric(5, 4)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_corrections_field_created", "garment_corrections", ["field_name", "created_at"]
    )
    op.create_index("ix_corrections_user", "garment_corrections", ["user_id", "created_at"])

    op.add_column(
        "garments",
        sa.Column(
            "moderation",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="NSFW score, verdict and model. Kept on the garment because "
            "the audit_log deliberately holds no per-image detail.",
        ),
    )

    # Tenant-scoped, so it gets RLS like every other table holding user data.
    op.execute("ALTER TABLE garment_corrections ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE garment_corrections FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON garment_corrections
          USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
          WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON garment_corrections TO stylist_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON garment_corrections")
    op.drop_index("ix_corrections_user", table_name="garment_corrections")
    op.drop_index("ix_corrections_field_created", table_name="garment_corrections")
    op.drop_table("garment_corrections")
    op.drop_column("garments", "moderation")
    op.drop_column("user_profile", "litellm_budget_usd")
    op.drop_column("user_profile", "litellm_key")
