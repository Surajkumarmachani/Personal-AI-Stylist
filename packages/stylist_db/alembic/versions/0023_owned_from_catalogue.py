"""Link a garment back to the catalogue product it came from (Phase 13).

WHY A COLUMN AND NOT A NOTE IN `attributes_raw`
------------------------------------------------
Two things need this to be queryable. The first is IDEMPOTENCE: tapping "I
bought this" twice, or a conversion feed replaying a click, must not put two
copies of the same pair of shoes in a wardrobe. The partial unique index below
is what actually enforces that — a check-then-insert in the router would race
with itself.

The second is PROVENANCE, and it matters more than it looks. A garment created
from a catalogue row has its slot, colour and dress code copied from merchant
data, not inferred from a photograph by our own model. Correction rate per
field is how this project measures tagging quality, and counting merchant
metadata as a model success would inflate that number with work the model
never did. `sourced_product_id IS NOT NULL` is how those rows get excluded.

ON DELETE SET NULL, NOT CASCADE
--------------------------------
A product leaving the catalogue (discontinued, supplier dropped) must not
delete a garment the user owns. They still own the shoes. The link goes, the
garment stays.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_owned_from_catalogue"
down_revision = "0022_catalogue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0022 documented this column as "'shown' or 'clicked'". A third kind
    # exists now, and a column comment that lists two of three values is worse
    # than none: it reads as authoritative.
    op.alter_column(
        "product_event",
        "kind",
        existing_type=sa.String(16),
        comment="'shown', 'clicked' or 'bought'. Clicks are what an affiliate "
        "programme pays on; impressions are what makes a click rate meaningful. "
        "'bought' is STATED BY THE USER, not observed — the checkout happens on "
        "the merchant's site.",
    )
    op.add_column(
        "garments",
        sa.Column("sourced_product_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_garments_sourced_product",
        "garments",
        "product",
        ["sourced_product_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # PARTIAL, so the millions of ordinary photographed garments (all NULL)
    # are not forced to be distinct from one another.
    op.create_index(
        "uq_garment_one_per_product",
        "garments",
        ["user_id", "sourced_product_id"],
        unique=True,
        postgresql_where=sa.text("sourced_product_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_garment_one_per_product", table_name="garments")
    op.drop_constraint("fk_garments_sourced_product", "garments", type_="foreignkey")
    op.drop_column("garments", "sourced_product_id")
