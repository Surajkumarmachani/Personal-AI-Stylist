"""Separate the embedding model's version from the tagger's

Revision ID: 0004_split_extractor_versions
Revises: 0003_vlm_tagging
Create Date: 2026-09-10

`garments.extractor_version` had TWO writers with different meanings. The tag
stage stamped "tag-vlm-v1"; the embed stage stamped
"embed-fashionsiglip-<sha>". embed runs after tag in INGEST_STAGES, so the
tagger's version was overwritten on every single ingest and the column ended up
describing the embedder.

That silently broke the two things the column exists for:

  - `garment_corrections.extractor_version` records what produced the value the
    user just corrected. Pointing at the embedder makes the correction log
    unable to answer "which tagger got this wrong", which §D1 calls the live
    accuracy metric.
  - §D1 makes extractor_version the trigger for a re-extraction backfill. A
    VLM upgrade left no trace in the column, so "re-tag everything tagged by
    the old model" could not be expressed as a query.

One column per model, because they version independently: a new embedder means
re-embed and reindex, a new tagger means re-tag. Conflating them means every
change to either looks like a change to both.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_split_extractor_versions"
down_revision = "0003_vlm_tagging"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "garments",
        sa.Column(
            "embedding_version",
            sa.String(64),
            nullable=True,
            comment="Model that produced garment_embeddings.vector. Separate from "
            "extractor_version, which now belongs to the tagger alone.",
        ),
    )
    # Backfill: any row whose extractor_version looks like the embedder's stamp
    # was written by the embed stage, so move it to the new column. The tag
    # version it overwrote is genuinely lost and cannot be reconstructed — it is
    # left NULL rather than guessed, so "unknown" stays distinguishable from a
    # real value.
    op.execute(
        """
        UPDATE garments
           SET embedding_version = extractor_version,
               extractor_version = NULL
         WHERE extractor_version LIKE 'embed-%'
        """
    )
    op.create_index(
        "ix_garments_embedding_version",
        "garments",
        ["embedding_version"],
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    # Put it back where it was, so the column's single-writer history is
    # preserved on the way down too.
    op.execute(
        """
        UPDATE garments
           SET extractor_version = embedding_version
         WHERE embedding_version IS NOT NULL
           AND extractor_version IS NULL
        """
    )
    op.drop_index("ix_garments_embedding_version", table_name="garments")
    op.drop_column("garments", "embedding_version")
