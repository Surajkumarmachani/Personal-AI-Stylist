"""Phase 6: materialised outfit suggestions

Revision ID: 0007_outfits
Revises: 0006_ops_aggregates
Create Date: 2026-09-11

WHY OUTFITS ARE STORED AND NOT COMPUTED PER REQUEST
---------------------------------------------------
The exit criterion is `GET /suggestions` at p95 < 300ms with zero model calls.
Candidate generation produces 200-500 combinations, each needing a pgvector
lookup per slot and a six-term score. That is not a 300ms request path for a
400-item wardrobe, and it would run the same work again on every refresh.

So the nightly precompute materialises the top ~200 scored outfits per tenant
and the request path becomes an indexed read. Invalidation is driven by outbox
events — a corrected garment, a new garment, a merge — because an outfit built
on tags the user has since fixed is worse than no suggestion.

`score_breakdown` IS NOT OPTIONAL
---------------------------------
The plan says "you cannot debug ranking without it", and Phase 6 has already
proved the point: with mock tags, three of six sub-scores carry no signal, and
the only way to see that from a stored outfit is the per-sub-score record. It
is JSONB rather than columns because the sub-score set changes by design —
style_affinity activates in Phase 8, trend_alignment in Phase 11.

`garment_ids` IS AN ARRAY, AND `garment_set_hash` IS ITS IDENTITY
-----------------------------------------------------------------
An outfit is a SET of garments, so the same three items must not be storable
twice in a different order. The hash is computed over the sorted ids, and the
unique index is on (user_id, garment_set_hash) — which also makes the Phase 7
rerank cache key from the plan ("garment_set_hash, occasion_bucket, ...")
available for free.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_outfits"
down_revision = "0006_ops_aggregates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outfits",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The garments, as a set. ON DELETE is not expressible for an array
        # element, so a deleted garment is handled by invalidation rather than
        # by a constraint — see the stale-outfit reaper in the precompute job.
        sa.Column(
            "garment_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
        ),
        # sha256 over the SORTED ids. Set identity, not list identity.
        sa.Column("garment_set_hash", sa.String(64), nullable=False),
        # What this outfit was built FOR. An outfit is only good relative to an
        # occasion and a temperature, so these are part of its identity for
        # retrieval, not metadata.
        sa.Column("occasion", sa.String(32), nullable=False),
        sa.Column(
            "warmth_target",
            sa.SmallInteger(),
            nullable=False,
            comment="Bucketed from feels_like via taxonomy warmth ceilings. "
            "Raw temperature would give a 0% cache hit rate (§6.4).",
        ),
        sa.Column("formality_target", sa.SmallInteger(), nullable=False),
        sa.Column("wet", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("score", sa.Numeric(8, 6), nullable=False),
        sa.Column(
            "score_breakdown",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Per-sub-score value, weight, contribution and whether the "
            "input carried any signal. Ranking is undebuggable without it.",
        ),
        # The scorer's config version, so a ranking change can be traced to the
        # weights that produced it rather than guessed at.
        sa.Column("scoring_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    # The retrieval path: newest, best-scoring outfits for a tenant and context.
    op.create_index(
        "ix_outfits_lookup",
        "outfits",
        ["user_id", "occasion", "warmth_target", sa.text("score DESC")],
    )
    # Set identity per tenant and context. The same three garments CAN appear
    # for a different occasion — a shirt and chinos is both office_casual and
    # dinner_date — so the context is part of the key.
    op.create_index(
        "uq_outfit_set_per_context",
        "outfits",
        ["user_id", "garment_set_hash", "occasion", "warmth_target"],
        unique=True,
    )

    op.execute("ALTER TABLE outfits ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE outfits FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON outfits
          USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
          WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON outfits TO stylist_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON outfits")
    op.drop_index("uq_outfit_set_per_context", table_name="outfits")
    op.drop_index("ix_outfits_lookup", table_name="outfits")
    op.drop_table("outfits")
