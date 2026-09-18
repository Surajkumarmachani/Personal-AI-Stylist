"""Per-tenant bandit posteriors (Phase 11).

DERIVED STATE, AND REBUILDABLE
------------------------------
Like `user_style_vector`, these counters are derived from `outfit_feedback` and
nothing else. `stylist_domain.bandit.apply_feedback` is a pure function of
(arm, kind), so the live update and a replay of the event log are one
implementation called twice — the property Phase 8 had to enforce for the
style vector after learning that unrebuildable derived state is a trap.

WHY COUNTS AND NOT A RATE
-------------------------
`successes` and `failures` rather than a stored probability. A Beta posterior
needs both to express CONFIDENCE: 1 like from 2 shows and 500 likes from 1000
have the same rate and must not be sampled alike. Storing a rate would throw
away exactly the information Thompson sampling runs on.

TENANT-SCOPED, unlike `trend_signal`. A bandit posterior is one person's
demonstrated taste — the most personal derived object in the system after the
style vector — so it gets RLS like every other tenant table. The contrast is
deliberate: trends are published aggregates that passed a k-anonymity floor,
these are not aggregates at all.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_bandit_arm"
down_revision = "0015_trend_signal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bandit_arm",
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "arm_key",
            sa.String(64),
            nullable=False,
            comment="The outfit kind. Currently dress_code; see stylist_domain.bandit.arm_key.",
        ),
        sa.Column("successes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "arm_key"),
        sa.CheckConstraint("successes >= 0 AND failures >= 0", name="ck_bandit_counts_positive"),
        comment="Thompson-sampling posteriors per outfit kind. DERIVED from "
        "outfit_feedback and rebuildable via stylist_domain.bandit.apply_feedback.",
    )

    op.execute("ALTER TABLE bandit_arm ENABLE ROW LEVEL SECURITY")
    # FORCE, so the table is not readable even by a role that owns it without
    # a tenant context — the same rule every other tenant table follows here.
    op.execute("ALTER TABLE bandit_arm FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON bandit_arm
        USING (user_id = current_setting('app.user_id', true)::uuid)
        WITH CHECK (user_id = current_setting('app.user_id', true)::uuid)
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON bandit_arm TO stylist_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON bandit_arm")
    op.drop_table("bandit_arm")
