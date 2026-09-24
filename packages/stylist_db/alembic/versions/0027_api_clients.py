"""API keys for partner servers, and the users they act for (Phase 14).

WHAT THIS ENABLES
-----------------
Another company's backend calls this API with no person logging in. It
authenticates with an API key and names which of ITS users a call is for
(`X-User-Id: cust_123`). That user is an ordinary row in `users`, so every
tenant-scoped endpoint — chat, suggestions, wardrobe, laundry, shop — works
for them unchanged, under the same RLS as everyone else.

A PARTNER CAN ONLY REACH ITS OWN USERS
--------------------------------------
`users.api_client_id` records which partner created an account, and the key
resolves a user only through `(api_client_id, external_id)`. There is no
header that names a user by our own id, so a partner cannot guess its way
into an account it did not create, nor into a person who signed up directly.

KEYS ARE STORED AS SHA-256, NOT BCRYPT
--------------------------------------
Passwords are bcrypt because people choose weak ones. An API key is 32 random
bytes; nobody brute-forces that, so a slow hash would only add ~100ms to every
partner request. The `prefix` is stored in clear so a key can be found without
scanning, and shown in listings so a partner can tell keys apart without the
secret ever being displayed again.

NOT TENANT DATA — NO RLS
------------------------
Keys and clients belong to the deployment, not to a user, and must be read
before any tenant is known (that is what they are for). Like `product`, they
are plain tables; only admins can create or revoke them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0027_api_clients"
down_revision = "0026_dresses_as"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_client",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column(
            "rate_limit_per_minute",
            sa.Integer(),
            nullable=False,
            server_default="120",
            comment="Requests per minute across ALL of this client's keys.",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("rate_limit_per_minute > 0", name="ck_api_client_rate_positive"),
        comment="A partner company calling server-to-server. No RLS — see 0027.",
    )
    op.create_table(
        "api_key",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "prefix",
            sa.String(16),
            nullable=False,
            comment="The key's public prefix, for lookup and for telling keys apart.",
        ),
        sa.Column("key_hash", sa.String(64), nullable=False, comment="sha256 hex of the full key."),
        sa.Column("label", sa.String(80), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["client_id"], ["api_client.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("prefix", name="uq_api_key_prefix"),
    )
    op.create_index("ix_api_key_client", "api_key", ["client_id"])

    op.add_column(
        "users",
        sa.Column("api_client_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "external_id",
            sa.String(128),
            nullable=True,
            comment="The partner's own id for this user. Unique per partner.",
        ),
    )
    op.create_foreign_key(
        "fk_users_api_client", "users", "api_client", ["api_client_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index(
        "uq_users_partner_external",
        "users",
        ["api_client_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("api_client_id IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_users_partner_pair",
        "users",
        "(api_client_id IS NULL) = (external_id IS NULL)",
    )

    op.execute("GRANT SELECT, INSERT, UPDATE ON api_client TO stylist_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON api_key TO stylist_app")


def downgrade() -> None:
    op.drop_constraint("ck_users_partner_pair", "users", type_="check")
    op.drop_index("uq_users_partner_external", table_name="users")
    op.drop_constraint("fk_users_api_client", "users", type_="foreignkey")
    op.drop_column("users", "external_id")
    op.drop_column("users", "api_client_id")
    op.drop_index("ix_api_key_client", table_name="api_key")
    op.drop_table("api_key")
    op.drop_table("api_client")
