"""Purchases from forwarded order emails (Phase 15).

A user forwards order confirmations from Myntra, AJIO, Amazon and the like to a
private address; the garment appears in their wardrobe with the store's photo
and details. See stylist_shop/order_email.py for the parsing and
services/stylist_api/routers/purchases.py for the inbound webhook.

TWO TABLES
----------
purchase_inbox  one private token per user. The inbound address is
                orders-<token>@<INBOUND_EMAIL_DOMAIN>; the token IS the
                authorisation, so it is random, unguessable and rotatable.
purchase_email  every email received, with what became of it: added, ignored
                (not a store), a Gmail forwarding-confirmation code the user
                must see, or a failure. The raw body is kept only until it has
                been processed, then cleared: an order email carries an address
                and a phone number, and nothing here needs them once parsed.

HOW THE WEBHOOK FINDS THE USER WITHOUT A LOGIN
-----------------------------------------------
Same shape as shop_ref_owner (0025): one SECURITY DEFINER function takes the
token and returns its owner, never a list. Everything after that lookup runs in
an ordinary tenant_session for that user, so RLS still governs every write.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0028_purchase_email"
down_revision = "0027_api_clients"
branch_labels = None
depends_on = None

TENANT_PREDICATE = "user_id = NULLIF(current_setting('app.user_id', true), '')::uuid"
STATUSES = ("received", "added", "nothing_to_add", "ignored", "gmail_confirmation", "failed")


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})"
    )


def upgrade() -> None:
    op.create_table(
        "purchase_inbox",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "token",
            sa.String(32),
            nullable=False,
            comment="The local part after 'orders-'. Random; rotating it retires the address.",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("token", name="uq_purchase_inbox_token"),
        comment="Tenant-scoped: each user's private order-forwarding address. RLS forced.",
    )
    _rls("purchase_inbox")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON purchase_inbox TO stylist_app")

    op.create_table(
        "purchase_email",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "message_id",
            sa.String(512),
            nullable=False,
            comment="The email's Message-ID. With user_id, the natural key: a "
            "provider retrying the webhook updates one row, not two.",
        ),
        sa.Column("sender", sa.String(320), nullable=False),
        sa.Column("subject", sa.String(998), nullable=False, server_default=""),
        sa.Column("store", sa.String(40), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="received"),
        sa.Column(
            "detail",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
            comment="What happened: items found, garments added, a Gmail code, or the error.",
        ),
        sa.Column(
            "body_html",
            sa.Text(),
            nullable=True,
            comment="Kept only until processed, then NULL. Carries PII nobody needs afterwards.",
        ),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "message_id", name="uq_purchase_email_message"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in STATUSES) + ")",
            name="ck_purchase_email_status",
        ),
        comment="Tenant-scoped: order emails forwarded by the user, and their outcome. RLS forced.",
    )
    op.create_index("ix_purchase_email_user", "purchase_email", ["user_id", "received_at"])
    _rls("purchase_email")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON purchase_email TO stylist_app")

    # The only privileged read: token in, owner out. search_path pinned for the
    # reason 0006 gives.
    op.execute(
        """
        CREATE FUNCTION purchase_inbox_owner(inbox_token text)
        RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT i.user_id FROM purchase_inbox i WHERE i.token = inbox_token
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION purchase_inbox_owner(text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION purchase_inbox_owner(text) TO stylist_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS purchase_inbox_owner(text)")
    op.drop_index("ix_purchase_email_user", table_name="purchase_email")
    op.drop_table("purchase_email")
    op.drop_table("purchase_inbox")
