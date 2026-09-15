"""Phase 8: outfit feedback, style vectors, preference facts

Revision ID: 0009_feedback_style
Revises: 0008_precompute_tenants
Create Date: 2026-09-15

THE EVENT LOG IS THE TRUTH; EVERYTHING ELSE IS A CACHE
------------------------------------------------------
`outfit_feedback` is APPEND-ONLY. No updates, no deletes, no "current
preference" column anywhere — a user who liked an outfit in March and disliked
it in June did both, and a table that stores only the latest verdict cannot
answer when their taste changed or replay the log to fix a scoring bug.

`user_style_vector` is therefore DERIVED STATE and is marked as such. The plan
is explicit: write `scripts/rebuild_style_vectors.py` in this phase "or you
will accumulate unrebuildable state". That script replays this table; the exit
criterion asserts the replay reproduces the live vector exactly. Which is only
possible because the EWMA is applied in a defined order — hence the
`(user_id, created_at, id)` index, and `id` in it: two events in the same
millisecond must still have one canonical order, or the replay and the live
update diverge by a rounding error nobody can explain.

WHY `reason` IS AN ENUM, NULLABLE, AND HAS NO `other`
-----------------------------------------------------
Free text would be richer and unusable — nobody aggregates 400 sentences, and
the whole point of capturing a reason is to turn "dislike" into something that
changes a score. Nullable because forcing a reason on every tap is how you get
a feedback button nobody presses.

There is deliberately no `other`, and `tests/test_taxonomy_enums.py` enforces
that across every enum in the database. An `other` bucket absorbs everything
that does not fit and then tells you nothing: a reason chosen 40% of the time
that maps to no action is worse than no reason, because it LOOKS like data. A
NULL is the honest encoding of "unspecified" and is already available here.

PREFERENCE FACTS ARE TYPED, AND USER-EDITABLE
---------------------------------------------
`avoids: crop_tops`, `never: yellow`. The plan's argument is that "legibility
buys trust faster than accuracy does": a user who can SEE what the system
believes about them, and correct it, forgives a wrong suggestion. A learned
embedding cannot be shown to anyone. These two live side by side on purpose —
the vector is what ranks, the facts are what the user can argue with.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0009_feedback_style"
down_revision = "0008_precompute_tenants"
branch_labels = None
depends_on = None

# EMBEDDING DIMENSION must match `garments.embedding` — the style vector is an
# average of garment embeddings and lives in the same space. Hardcoding a
# different number here would fail only at the first write, in production.
EMBED_DIM = 768


def upgrade() -> None:
    feedback_kind = postgresql.ENUM(
        "like",
        "dislike",
        "worn",
        "dismissed",
        "saved",
        name="feedback_kind",
        create_type=True,
    )
    feedback_reason = postgresql.ENUM(
        "too_formal",
        "too_casual",
        "too_warm",
        "too_cold",
        "colours_clash",
        "not_my_style",
        "in_laundry",
        "wrong_occasion",
        name="feedback_reason",
        create_type=True,
    )
    fact_kind = postgresql.ENUM(
        "avoids",
        "never",
        "prefers",
        name="preference_fact_kind",
        create_type=True,
    )

    # ---------------------------------------------------------- feedback
    op.create_table(
        "outfit_feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The outfit AS IT WAS SUGGESTED, denormalised on purpose. `outfits` is
        # rebuilt nightly and pruned, so a foreign key would delete the
        # feedback history every time the precompute ran — destroying the only
        # record of what the user actually reacted to.
        sa.Column(
            "garment_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
        ),
        sa.Column("garment_set_hash", sa.String(64), nullable=False),
        sa.Column("occasion", sa.String(32), nullable=True),
        sa.Column("kind", feedback_kind, nullable=False),
        sa.Column("reason", feedback_reason, nullable=True),
        # Whether this outfit was SUGGESTED by us or assembled by the user.
        # Wear-through rate — "the only quality metric that matters" — is
        # worn-and-suggested over suggested, and is unanswerable without it.
        sa.Column(
            "was_suggested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    # Replay order. `id` is the tie-break so that two events sharing a
    # timestamp still have ONE canonical order — without it the rebuild script
    # and the live EWMA can disagree, and the exit criterion asserts they
    # cannot.
    op.create_index("ix_feedback_replay", "outfit_feedback", ["user_id", "created_at", "id"])
    # Wear-through and coverage queries: "of what we suggested, what got worn".
    op.create_index(
        "ix_feedback_kind", "outfit_feedback", ["user_id", "kind", sa.text("created_at DESC")]
    )

    # ------------------------------------------------------ style vector
    op.create_table(
        "user_style_vector",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # pgvector, not float[], and in the SAME space as `garments.embedding`
        # (migration 0002). The style vector's whole job is to be compared to
        # garment embeddings — `style_affinity` is a cosine — and a float array
        # would force a cast on every comparison, silently giving up the HNSW
        # index that makes it cheap.
        sa.Column("vector", Vector(EMBED_DIM), nullable=False),
        # DERIVED STATE, and these three columns are what make it auditable.
        # `events_applied` and `last_event_id` let the rebuild script prove it
        # replayed the same log; `alpha` records the decay the live vector was
        # built with, so changing the constant does not silently invalidate
        # every stored vector without anyone noticing.
        sa.Column("events_applied", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("alpha", sa.Numeric(4, 3), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    # ------------------------------------------------- preference facts
    op.create_table(
        "preference_fact",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", fact_kind, nullable=False),
        # The taxonomy field this is about (`subcategory`, `primary_colour`),
        # and the value within it. Two columns rather than a free-text string
        # so a fact can be APPLIED as a filter, not just displayed.
        sa.Column("field_name", sa.String(32), nullable=False),
        sa.Column("field_value", sa.String(64), nullable=False),
        # Did the user assert this, or did we infer it? An inferred fact the
        # user has not seen must never be treated as certain, and the UI shows
        # the two differently — inferring "never: yellow" from two dislikes and
        # presenting it as the user's own words is how you lose their trust.
        sa.Column(
            "source",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'user'"),
            comment="'user' (asserted) or 'inferred'. Never conflate them.",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "uq_preference_fact",
        "preference_fact",
        ["user_id", "kind", "field_name", "field_value"],
        unique=True,
    )

    for table in ("outfit_feedback", "user_style_vector", "preference_fact"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
              USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
              WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
            """
        )

    # NO UPDATE OR DELETE ON `outfit_feedback`, ENFORCED BY REVOKE.
    #
    # Append-only is a property the database should hold, not a rule the
    # handlers promise to follow. A handler that forgets is a bug; a missing
    # privilege is an error. This is the table every derived thing replays, so
    # a single silent UPDATE makes the rebuild script's output disagree with
    # live state for a reason nobody can find afterwards.
    #
    # IT MUST BE A REVOKE, NOT A NARROWER GRANT. Migration 0001 sets
    # `ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE,
    # DELETE ON TABLES TO stylist_app`, so every table created afterwards
    # arrives with the full set already attached. `GRANT SELECT, INSERT` here
    # adds nothing and removes nothing — it reads like a restriction and is a
    # no-op. Verified the wrong way round first: the grant was in place and
    # `UPDATE outfit_feedback` still succeeded as `stylist_app`.
    # ---------------------------------------------------- tenant driver
    #
    # `rebuild_style_vectors.py` has to enumerate tenants BEFORE it can set a
    # tenant context, and it runs as `stylist_app` against FORCE-RLS tables —
    # so `SELECT DISTINCT user_id FROM outfit_feedback` returns ZERO ROWS with
    # no error. The script would then report "0 tenants, all consistent",
    # indistinguishable from success. That exact failure has already happened
    # twice here: the Phase 5 ops alerts and the Phase 6 precompute driver.
    #
    # Same shape as `precompute_tenants()` (0008): the function runs with the
    # owner's rights and returns nothing but a set of uuids, so the app role
    # gains no ability to read a single feedback row belonging to anyone.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION feedback_tenants()
        RETURNS TABLE (user_id uuid)
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT DISTINCT f.user_id FROM outfit_feedback f
        $$
        """
    )
    # CREATE FUNCTION grants EXECUTE to PUBLIC by default, which would hand the
    # definer's read access to every role in the database.
    op.execute("REVOKE ALL ON FUNCTION feedback_tenants() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION feedback_tenants() TO stylist_app")

    op.execute("REVOKE UPDATE, DELETE ON outfit_feedback FROM stylist_app")
    op.execute("GRANT SELECT, INSERT ON outfit_feedback TO stylist_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON user_style_vector TO stylist_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON preference_fact TO stylist_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS feedback_tenants()")
    for table in ("preference_fact", "user_style_vector", "outfit_feedback"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_index("uq_preference_fact", table_name="preference_fact")
    op.drop_table("preference_fact")
    op.drop_table("user_style_vector")
    op.drop_index("ix_feedback_kind", table_name="outfit_feedback")
    op.drop_index("ix_feedback_replay", table_name="outfit_feedback")
    op.drop_table("outfit_feedback")
    for enum in ("preference_fact_kind", "feedback_reason", "feedback_kind"):
        op.execute(f"DROP TYPE IF EXISTS {enum}")
