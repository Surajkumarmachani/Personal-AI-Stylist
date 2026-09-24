"""Purchases reported by the merchant, so a bought item joins the wardrobe itself.

WHAT CHANGES
------------
Until now "I bought this" was the ONLY way a catalogue product became a
garment, because the checkout happens on the merchant's site and we never see
it. Affiliate networks do see it: they report each order back to a postback
URL we register with them, carrying a reference we put on the outbound link.
That reference is the `product_event` row that SHOWED the product — so the
report says, in effect, "the person who saw product P bought it".

HOW THE POSTBACK FINDS ITS TENANT WITHOUT READING PAST RLS
----------------------------------------------------------
The postback is a server-to-server call with no user token, and
`product_event` is RLS-forced. A second, privileged connection would put owner
credentials in the internet-facing process (see 0006 for why that is refused).

So one SECURITY DEFINER function answers one question — which user and product
does this reference belong to? — and nothing else. The caller must already
hold the reference, which is a random uuid printed only on that user's link,
so the function reveals nothing the caller did not already have. Everything
after the lookup runs in an ordinary `tenant_session` for that user.

IDEMPOTENT ON (network, conversion_id)
--------------------------------------
Networks retry postbacks and resend them when an order changes state
(pending -> approved, or -> rejected on a return). The unique key turns a
replay into an update of one row rather than a second garment.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025_shop_conversions"
down_revision = "0024_more_occasions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shop_conversion",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "ref",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="The product_event id printed on the outbound link.",
        ),
        sa.Column("network", sa.String(40), nullable=False),
        sa.Column(
            "conversion_id",
            sa.String(128),
            nullable=False,
            comment="The network's own order/conversion id. With `network`, the natural key.",
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            comment="Normalised: 'pending', 'approved' or 'rejected'. Rejected is a "
            "cancellation or return — the user no longer owns it.",
        ),
        sa.Column("garment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("raw", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["product.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["garment_id"], ["garments.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("network", "conversion_id", name="uq_shop_conversion_source"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')", name="ck_shop_conversion_status"
        ),
        comment="Tenant-scoped: orders a merchant reported for this user. RLS forced.",
    )
    op.create_index("ix_shop_conversion_user", "shop_conversion", ["user_id", "created_at"])

    op.execute("ALTER TABLE shop_conversion ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE shop_conversion FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON shop_conversion
        USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON shop_conversion TO stylist_app")

    # The only privileged read. Takes a reference, returns its owner and
    # product — never a list, never another user's rows. `search_path` pinned
    # for the reason 0006 gives: without it the definer can be made to run a
    # caller's same-named object.
    op.execute(
        """
        CREATE FUNCTION shop_ref_owner(ref uuid)
        RETURNS TABLE (user_id uuid, product_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT e.user_id, e.product_id FROM product_event e
            WHERE e.id = ref AND e.kind IN ('shown', 'clicked')
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION shop_ref_owner(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION shop_ref_owner(uuid) TO stylist_app")

    op.alter_column(
        "product_event",
        "kind",
        existing_type=sa.String(16),
        comment="'shown', 'clicked' or 'bought'. A 'shown' row's id is printed on "
        "the outbound link as the affiliate reference, so a merchant's postback "
        "can name the user without a token. 'bought' is stated by the user OR "
        "reported by the merchant — shop_conversion says which.",
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS shop_ref_owner(uuid)")
    op.drop_index("ix_shop_conversion_user", table_name="shop_conversion")
    op.drop_table("shop_conversion")
