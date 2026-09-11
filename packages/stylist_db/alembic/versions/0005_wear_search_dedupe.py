"""Phase 5: wear log, laundry state, full-text search, duplicate linkage

Revision ID: 0005_wear_search_dedupe
Revises: 0004_split_extractor_versions
Create Date: 2026-09-11

Four additions, one per Phase 5 deliverable that needs storage.

`wear_log` — append-only, one row per wearing. Not a counter on `garments`:
cost-per-wear, "your 20 most-worn" and "not worn in 90 days" are all questions
about WHEN, and a counter answers none of them. It also makes an accidental
double-tap correctable by deleting a row rather than decrementing a number that
may already be wrong.

`garments.needs_wash` — laundry state. A boolean rather than a state machine
because the real world has exactly two interesting answers here, and the outfit
suggester in Phase 6 only needs to exclude what is in the basket.

`garments.search_text` — a tsvector maintained by a BEFORE INSERT OR UPDATE
trigger. A GENERATED column was the first choice and Postgres rejects it:
casting an enum to text is not immutable, and every searchable field here is an
enum. The trigger gives the same guarantee — the search document is rewritten
by the same statement that changes the garment, so a correction cannot leave
it stale — it just enforces it in a trigger rather than in the column
definition. What both rule out is the drift you get from recomputing search
text at the call site, where one forgotten UPDATE silently makes a garment
unfindable.

`garments.duplicate_of` — where a DUPLICATE_SUSPECT points. Nullable and
advisory: the pipeline never merges, it only proposes, so this is a question
awaiting an answer rather than a decision already taken.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_wear_search_dedupe"
down_revision = "0004_split_extractor_versions"
branch_labels = None
depends_on = None

# The searchable document. Enum columns are cast to text because tsvector input
# must be text, and every field is coalesced because a garment whose tagging
# degraded still has to be findable by the fields it DOES have.
SEARCH_DOC = """
    to_tsvector('english',
        coalesce(NEW.subcategory::text, '') || ' ' ||
        coalesce(NEW.slot::text, '') || ' ' ||
        coalesce(NEW.primary_colour::text, '') || ' ' ||
        coalesce(NEW.secondary_colour::text, '') || ' ' ||
        coalesce(NEW.material::text, '') || ' ' ||
        coalesce(NEW.pattern::text, '') || ' ' ||
        coalesce(NEW.fit::text, '') || ' ' ||
        coalesce(NEW.dress_code::text, '')
    )
"""


def upgrade() -> None:
    # ---- wear log -------------------------------------------------------
    op.create_table(
        "wear_log",
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
        # DATE, not timestamp: "I wore this on Tuesday" is the fact being
        # recorded. A timestamp would invite a precision the user never
        # supplied and make "worn today" depend on their timezone.
        sa.Column("worn_on", sa.Date(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # One wearing per garment per day. Re-tapping "worn today" is a duplicate
    # of a fact, not a second fact, and cost-per-wear is wrong if it counts.
    op.create_index("uq_wear_once_per_day", "wear_log", ["garment_id", "worn_on"], unique=True)
    # Serves "most worn" and "not worn since" without touching the garment row.
    op.create_index("ix_wear_user_date", "wear_log", ["user_id", "worn_on"])

    op.execute("ALTER TABLE wear_log ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE wear_log FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON wear_log
          USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
          WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON wear_log TO stylist_app")

    # ---- laundry + cost-per-wear ---------------------------------------
    op.add_column(
        "garments",
        sa.Column("needs_wash", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "garments",
        sa.Column(
            "purchase_price_minor",
            sa.BigInteger(),
            nullable=True,
            comment="Minor units (paise/cents). Integer, never float: money in "
            "binary floating point accumulates error, and cost-per-wear divides it.",
        ),
    )
    op.add_column(
        "garments",
        sa.Column("purchase_currency", sa.String(3), nullable=True, server_default="INR"),
    )

    # ---- duplicate linkage ---------------------------------------------
    op.add_column(
        "garments",
        sa.Column(
            "duplicate_of",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("garments.id", ondelete="SET NULL"),
            nullable=True,
            comment="Proposed duplicate. ADVISORY: the pipeline never merges, "
            "it asks. Cleared when the user says they are different.",
        ),
    )

    # ---- search ---------------------------------------------------------
    op.add_column("garments", sa.Column("search_text", sa.dialects.postgresql.TSVECTOR))
    op.execute(
        f"""
        CREATE FUNCTION garments_search_text() RETURNS trigger AS $$
        BEGIN
            NEW.search_text := {SEARCH_DOC};
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_garments_search_text
        BEFORE INSERT OR UPDATE ON garments
        FOR EACH ROW EXECUTE FUNCTION garments_search_text()
        """
    )
    # Backfill rows that predate the trigger. A no-op UPDATE fires it.
    op.execute("UPDATE garments SET updated_at = updated_at")
    op.execute("CREATE INDEX ix_garments_search ON garments USING GIN (search_text)")
    # Dedupe scans by phash within a tenant, so the tenant must lead the index.
    op.execute(
        "CREATE INDEX ix_garments_user_phash ON garments (user_id, phash) "
        "WHERE is_active AND phash IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_garments_user_phash")
    op.execute("DROP INDEX IF EXISTS ix_garments_search")
    op.execute("DROP TRIGGER IF EXISTS trg_garments_search_text ON garments")
    op.execute("DROP FUNCTION IF EXISTS garments_search_text()")
    op.drop_column("garments", "search_text")
    op.drop_column("garments", "duplicate_of")
    op.drop_column("garments", "purchase_currency")
    op.drop_column("garments", "purchase_price_minor")
    op.drop_column("garments", "needs_wash")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON wear_log")
    op.drop_index("ix_wear_user_date", table_name="wear_log")
    op.drop_index("uq_wear_once_per_day", table_name="wear_log")
    op.drop_table("wear_log")
