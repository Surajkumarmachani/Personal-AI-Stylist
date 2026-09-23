"""A product catalogue, so the app can fill a gap the wardrobe cannot (Phase 13).

WHAT THIS IS FOR, AND THE LINE IT MUST NOT CROSS
------------------------------------------------
The product's claim, printed on its own screens, is "every look is built from
clothes you own". A shopping feed that sits alongside that and says "buy this
instead" erodes the only thing the app does that a retailer does not.

So the catalogue exists to answer ONE question: your wardrobe cannot dress
this occasion — what single item would fix it? That is a gap the system can
already identify precisely, because `prefer_one`/`required_slots` and the
candidate filters know exactly which slot came back empty and why (no
footwear, nothing in the right dress code, nothing warm enough).

NOT TENANT DATA — NO RLS
------------------------
A product is public catalogue content, identical for every user, so this table
has no `user_id` and no row-level security. The tenant-scoped half is
`product_event` below: which user saw and clicked what.

That split is deliberate and matches `trend_signal` (published aggregate, no
RLS) versus `bandit_arm` (one person's taste, RLS forced). Putting impressions
in the catalogue table would have made every product row personal data.

PRICES IN MINOR UNITS
---------------------
`price_minor` + `currency`, exactly like `garments.purchase_price_minor`.
Storing money as a float is how rounding errors become support tickets, and
the two tables are compared directly when cost-per-wear is estimated for a
thing you do not own yet.

MERCHANT-AGNOSTIC BY CONSTRUCTION
---------------------------------
`merchant` and `external_id` identify the row at its source; nothing else in
the schema assumes Amazon, Myntra or a CSV. The adapter that fills this table
is `packages/stylist_shop/providers.py`, and swapping one for another must not
require a migration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0022_catalogue"
down_revision = "0021_brand_size"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant", sa.String(40), nullable=False),
        sa.Column(
            "external_id",
            sa.String(128),
            nullable=False,
            comment="The merchant's own id. With `merchant`, the natural key.",
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("brand", sa.String(80), nullable=True),
        # The SAME enums garments use. A product that cannot be described in
        # the wardrobe's vocabulary cannot be matched against a wardrobe gap,
        # so the vocabulary is the integration contract.
        sa.Column("slot", postgresql.ENUM(name="slot", create_type=False), nullable=False),
        sa.Column(
            "subcategory", postgresql.ENUM(name="subcategory", create_type=False), nullable=True
        ),
        sa.Column(
            "primary_colour", postgresql.ENUM(name="colour", create_type=False), nullable=True
        ),
        sa.Column(
            "dress_code", postgresql.ENUM(name="dress_code", create_type=False), nullable=True
        ),
        sa.Column("formality", sa.SmallInteger(), nullable=True),
        sa.Column("warmth", sa.SmallInteger(), nullable=True),
        sa.Column("price_minor", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column(
            "in_stock",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="Stale stock is the fastest way to lose trust: a link to a "
            "sold-out item is worse than no link.",
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("merchant", "external_id", name="uq_product_source"),
        sa.CheckConstraint(
            "price_minor IS NULL OR price_minor >= 0", name="ck_product_price_non_negative"
        ),
        comment="Public catalogue content. No user_id and no RLS — see 0022.",
    )
    # The query that matters: "a `feet` item, casual, warmth 2-4, in stock".
    op.create_index(
        "ix_product_match",
        "product",
        ["slot", "dress_code", "in_stock"],
    )

    op.create_table(
        "product_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "kind",
            sa.String(16),
            nullable=False,
            comment="'shown' or 'clicked'. Clicks are what an affiliate "
            "programme pays on; impressions are what makes a click rate "
            "meaningful — a click count with no denominator says nothing.",
        ),
        sa.Column(
            "occasion",
            sa.String(32),
            nullable=True,
            comment="Which gap this was shown for, so the suggestion can be judged.",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["product.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        comment="Tenant-scoped: who saw and clicked what. RLS forced.",
    )
    op.create_index("ix_product_event_user", "product_event", ["user_id", "created_at"])

    op.execute("ALTER TABLE product_event ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE product_event FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON product_event
        USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        """
    )
    # The catalogue is readable by everyone; events are not.
    op.execute("GRANT SELECT ON product TO stylist_app")
    op.execute("GRANT SELECT, INSERT ON product_event TO stylist_app")


def downgrade() -> None:
    op.drop_table("product_event")
    op.drop_index("ix_product_match", table_name="product")
    op.drop_table("product")
