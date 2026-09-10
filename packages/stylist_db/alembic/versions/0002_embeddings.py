"""garment embeddings: pgvector column + HNSW index

Revision ID: 0002_embeddings
Revises: 0001_initial
Create Date: 2026-09-10

WHAT THE HNSW INDEX IS ACTUALLY FOR
-----------------------------------
Not wardrobe search. Wardrobe retrieval filters to one tenant — at most a few
hundred rows — where a sequential scan with an exact cosine distance beats any
approximate index and gives perfect recall for free. §B2 says so explicitly.

The index exists for cross-tenant style similarity (Phase 11) and it is sized
by that: 7.5M vectors x 768 dims x 4 bytes is ~23GB, which is the number that
decides the instance class. Two failure modes to guard against, both of them
people being reasonable:

  - someone "optimises away" an index nothing appears to use, and Phase 11's
    similarity feature silently falls back to a 7.5M-row scan.
  - someone assumes this index is what makes the wardrobe fast, and tunes it
    for a query shape that never touches it.

CREATE INDEX CONCURRENTLY, PER §D2
----------------------------------
The migration policy says "No blocking DDL on tables >1M rows. CREATE INDEX
CONCURRENTLY only." The table is empty today, so a plain CREATE INDEX would
work — and would also install the habit that breaks the first time this runs
against real data. CONCURRENTLY cannot run inside a transaction, hence the
autocommit block.

The cost of CONCURRENTLY is that a failure leaves an INVALID index behind
rather than rolling back, so the downgrade drops it unconditionally.
"""

from __future__ import annotations

from alembic import op

revision = "0002_embeddings"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

EMBEDDING_DIM = 768  # must equal stylist_ml.registry.EMBEDDING_DIM


def upgrade() -> None:
    # Deliberately here rather than in 0001: Phase 1 had no vectors, and an
    # extension the schema does not use is an extension that cannot be created
    # on a plain Postgres for no benefit. The compose image ships pgvector and
    # postgres-init also creates it, so this is idempotent in every
    # environment.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Nullable, and it stays nullable. A garment is VISIBLE to the user well
    # before it is embedded — partial usefulness is the point of the ingest
    # state machine — so NOT NULL here would either block that or force a
    # placeholder vector, and a placeholder vector is worse than a null: it
    # would participate in similarity search and match things.
    op.execute(f"ALTER TABLE garments ADD COLUMN embedding vector({EMBEDDING_DIM})")

    with op.get_context().autocommit_block():
        # m=16, ef_construction=64 are pgvector's documented defaults and the
        # plan's stated values. PROVISIONAL: retune in P9 — the trade is index
        # build time and memory against recall, and it cannot be tuned
        # meaningfully against an empty table.
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS garments_embedding_hnsw
            ON garments USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            """
        )

        # Partial index for the query the wardrobe ACTUALLY runs: "this
        # tenant's active garments in this slot". Partial on is_active because
        # soft-deleted rows are never listed, so indexing them wastes space
        # and slows every insert.
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS garments_user_slot_embedded
            ON garments (user_id, slot)
            WHERE is_active AND embedding IS NOT NULL
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        # Unconditional: a failed CONCURRENTLY build leaves an INVALID index
        # that a plain DROP INDEX still needs to clean up.
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS garments_user_slot_embedded")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS garments_embedding_hnsw")

    op.execute("ALTER TABLE garments DROP COLUMN IF EXISTS embedding")
    # The extension is NOT dropped: another schema in the same database may be
    # using it, and dropping a shared extension during a rollback is how you
    # turn one service's rollback into another service's outage.
