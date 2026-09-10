"""initial schema: taxonomy enums, core tables, RLS

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-09

Two things in here are load-bearing and easy to get wrong:

1. WITH CHECK is written explicitly even though Postgres does not require it.
   `USING` filters which rows a statement can SEE; `WITH CHECK` constrains which
   rows it may WRITE. When WITH CHECK is omitted, Postgres reuses the USING
   expression as the write check — verified against PG 17: a USING-only policy
   still rejects an INSERT stamped with another tenant's user_id. So this is
   NOT a latent write-side leak, and anyone who tells you otherwise (I did, at
   first) is wrong.

   It is spelled out anyway because the two rules should be free to diverge.
   The moment someone narrows USING — `AND is_active IS TRUE` is the obvious
   future edit — an implicit check would silently start rejecting INSERTs of
   inactive rows, coupling read visibility to write legality by accident.
   Explicit clauses make that decision deliberate instead of emergent.

2. NULLIF around current_setting. `current_setting('app.user_id', true)` returns
   NULL when unset (the `true` is missing_ok) but returns the empty string in
   some pooled/reset paths, and ''::uuid raises 22P02 rather than filtering.
   NULLIF(...,'')::uuid degrades to NULL, the comparison is NULL, and the policy
   denies. Fail-closed, no exception.

Note there is no `CREATE EXTENSION vector` here. Embeddings arrive in Phase 3;
adding the extension now would mean the schema cannot be created on a plain
Postgres, for no Phase 1 benefit. Expand-migrate-contract applies to extensions
too.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from stylist_db.taxonomy_enums import create_type_statements, drop_type_statements

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

# Tables that hold tenant data and therefore get a policy. Kept in sync with
# stylist_db.models.TENANT_SCOPED_TABLES by tests/test_rls_isolation.py.
TENANT_TABLES = ("user_profile", "garments", "jobs")

TENANT_PREDICATE = "user_id = NULLIF(current_setting('app.user_id', true), '')::uuid"


def upgrade() -> None:
    # ---- 1. enum types, generated from config/taxonomy.yaml ---------------
    for statement in create_type_statements():
        op.execute(statement)

    # ---- 2. tables --------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "user_profile",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("display_name", sa.String(120)),
        sa.Column("home_lat_2dp", sa.Numeric(5, 2)),
        sa.Column("home_lon_2dp", sa.Numeric(5, 2)),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Kolkata"),
        sa.Column("locale", sa.String(16), nullable=False, server_default="en-IN"),
        sa.Column(
            "preference_facts",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "garments",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("original_key", sa.Text, nullable=False),
        sa.Column("cutout_key", sa.Text),
        sa.Column("phash", sa.String(64)),
        # taxonomy columns use the generated enum types
        sa.Column("slot", sa.dialects.postgresql.ENUM(name="slot", create_type=False)),
        sa.Column(
            "subcategory", sa.dialects.postgresql.ENUM(name="subcategory", create_type=False)
        ),
        sa.Column("primary_colour", sa.dialects.postgresql.ENUM(name="colour", create_type=False)),
        sa.Column(
            "secondary_colour", sa.dialects.postgresql.ENUM(name="colour", create_type=False)
        ),
        sa.Column("pattern", sa.dialects.postgresql.ENUM(name="pattern", create_type=False)),
        sa.Column("material", sa.dialects.postgresql.ENUM(name="material", create_type=False)),
        sa.Column("fit", sa.dialects.postgresql.ENUM(name="fit", create_type=False)),
        sa.Column("dress_code", sa.dialects.postgresql.ENUM(name="dress_code", create_type=False)),
        sa.Column(
            "climate_bands",
            sa.dialects.postgresql.ARRAY(
                sa.dialects.postgresql.ENUM(name="climate_band", create_type=False)
            ),
        ),
        sa.Column("formality", sa.SmallInteger),
        sa.Column("warmth", sa.SmallInteger),
        sa.Column(
            "attributes_raw",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "field_confidence",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "user_verified_fields",
            sa.dialects.postgresql.ARRAY(sa.Text),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("extractor_version", sa.String(32)),
        sa.Column("state", sa.String(32), nullable=False, server_default="received"),
        sa.Column("needs_review", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("formality IS NULL OR formality BETWEEN 1 AND 5", name="ck_formality"),
        sa.CheckConstraint("warmth IS NULL OR warmth BETWEEN 1 AND 5", name="ck_warmth"),
    )
    op.create_index("ix_garments_user_slot_active", "garments", ["user_id", "slot", "is_active"])
    op.create_index("ix_garments_user_created", "garments", ["user_id", "created_at"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False, server_default="ingest"),
        sa.Column("state", sa.String(32), nullable=False, server_default="received"),
        sa.Column(
            "garment_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("garments.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "payload",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "stage_attempts",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("last_error", sa.Text),
        sa.Column("dlq_at", sa.DateTime(timezone=True)),
        sa.Column("idempotency_key", sa.String(255)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_jobs_user_idempotency"),
    )
    op.create_index("ix_jobs_user_state", "jobs", ["user_id", "state"])

    # outbox is intentionally NOT tenant-scoped: the relay runs without tenant
    # context and must see every tenant's unsent rows (§C1).
    op.create_table(
        "outbox",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("aggregate_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column(
            "payload",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
    )
    op.execute("CREATE INDEX ix_outbox_unsent ON outbox (created_at) WHERE sent_at IS NULL")

    op.create_table(
        "processed_keys",
        sa.Column("idempotency_key", sa.String(255), primary_key=True),
        sa.Column("consumer", sa.String(64), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "model_calls",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.Column("job_id", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.Column("model_name", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(64)),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("prompt_tokens", sa.Integer),
        sa.Column("completion_tokens", sa.Integer),
        sa.Column("cost_usd", sa.Numeric(12, 6)),
        sa.Column("latency_ms", sa.Integer),
        sa.Column("cache_hit", sa.Boolean),
        sa.Column("trace_id", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_model_calls_user_created", "model_calls", ["user_id", "created_at"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("subject_type", sa.String(64)),
        sa.Column("subject_id", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.Column(
            "detail",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("trace_id", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    # ---- 3. row level security -------------------------------------------
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE applies the policy to the table OWNER too. Without it, the role
        # that owns the table bypasses RLS entirely, which would make the whole
        # mechanism decorative the moment migrations and the app share a role.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
              USING ({TENANT_PREDICATE})
              WITH CHECK ({TENANT_PREDICATE})
            """
        )

    # ---- 4. least-privilege application role ------------------------------
    # RLS is bypassed by superusers and by any role with BYPASSRLS, so the app
    # MUST NOT connect as the migration/owner role. Created here (idempotently)
    # so a fresh database is correct without a manual step; the password comes
    # from the environment in every non-local environment.
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'stylist_app') THEN
            CREATE ROLE stylist_app LOGIN PASSWORD 'stylist_app_local_only'
              NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
          END IF;
        END
        $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO stylist_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO stylist_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO stylist_app")
    # Future tables created by later migrations inherit the same grants.
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
          GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO stylist_app
        """
    )


def downgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_table("audit_log")
    op.drop_index("ix_model_calls_user_created", table_name="model_calls")
    op.drop_table("model_calls")
    op.drop_table("processed_keys")
    op.execute("DROP INDEX IF EXISTS ix_outbox_unsent")
    op.drop_table("outbox")
    op.drop_index("ix_jobs_user_state", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_garments_user_created", table_name="garments")
    op.drop_index("ix_garments_user_slot_active", table_name="garments")
    op.drop_table("garments")
    op.drop_table("user_profile")
    op.drop_table("users")

    for statement in drop_type_statements():
        op.execute(statement)
