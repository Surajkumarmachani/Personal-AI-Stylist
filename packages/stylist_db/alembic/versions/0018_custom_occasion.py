"""User-named occasions that resolve to a taxonomy one (Phase 12).

WHY THIS IS AN ALIAS TABLE AND NOT A NEW ENUM VALUE
---------------------------------------------------
`taxonomy.yaml` is frozen at v1.0.0 and its enums GENERATE POSTGRES TYPES.
Adding a nineteenth occasion means `ALTER TYPE` on a type that garment columns
depend on, plus a re-freeze of a file whose whole purpose is not to move. That
is a large, irreversible change to satisfy "I want to call this one thing".

So a custom occasion is NOT a new occasion. It is a user's NAME for an
existing one, with optional nudges:

    "Haldi at the farmhouse"  ->  base mehendi
    "Friday standup"          ->  base wfh, formality 2

`base_occasion` is a plain varchar, not the taxonomy enum, and is validated in
the application against `taxonomy.occasions`. A foreign key onto a generated
enum type would couple this table to the freeze it exists to avoid; the
validation lives where the taxonomy is already loaded.

`resolve_context` therefore always receives a real taxonomy id and never
learns that aliases exist. The scorer, the precompute, the bandit and the
rerankers are untouched — which is the point. A feature that needs no change
to the ranking path cannot break the ranking path.

THE OVERRIDES ARE NULLABLE AND CHECKED
--------------------------------------
`formality_override` is 1-5, the taxonomy's own scale, and NULL means "use the
base occasion's". `dress_code_override` is likewise a varchar validated in the
application. Storing an out-of-range formality would reach the scorer as a
target no garment can match, which presents as "no outfits" with no reason —
so the range is a CHECK constraint rather than a convention.

UNIQUE ON (user_id, lower(name))
--------------------------------
Two aliases differing only in case are the same alias to the person who typed
them, and the intent lexicon lower-cases before matching — so without this the
second one would silently never win.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_custom_occasion"
down_revision = "0017_home_place"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "custom_occasion",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "name",
            sa.String(60),
            nullable=False,
            comment="The user's own words, e.g. 'Haldi at the farmhouse'.",
        ),
        sa.Column(
            "base_occasion",
            sa.String(32),
            nullable=False,
            comment="A real taxonomy occasion id. Validated in the application against "
            "taxonomy.occasions — NOT a FK onto the generated enum, deliberately.",
        ),
        sa.Column(
            "formality_override",
            sa.SmallInteger(),
            nullable=True,
            comment="1-5 on the taxonomy scale. NULL means use the base occasion's.",
        ),
        sa.Column(
            "dress_code_override",
            sa.String(32),
            nullable=True,
            comment="A taxonomy dress_code id. NULL means use the base occasion's.",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "formality_override IS NULL OR (formality_override BETWEEN 1 AND 5)",
            name="ck_custom_occasion_formality_range",
        ),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_custom_occasion_name_not_blank"),
        comment="A user's name for an existing taxonomy occasion, with optional "
        "formality/dress-code nudges. See migration 0018 for why this is an "
        "alias table rather than a nineteenth enum value.",
    )
    op.create_index(
        "uq_custom_occasion_name",
        "custom_occasion",
        ["user_id", sa.text("lower(name)")],
        unique=True,
    )

    op.execute("ALTER TABLE custom_occasion ENABLE ROW LEVEL SECURITY")
    # FORCE, like every other tenant table here: not readable even by an owner
    # role without a tenant context.
    op.execute("ALTER TABLE custom_occasion FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON custom_occasion
        USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON custom_occasion TO stylist_app")


def downgrade() -> None:
    op.drop_index("uq_custom_occasion_name", table_name="custom_occasion")
    op.drop_table("custom_occasion")
