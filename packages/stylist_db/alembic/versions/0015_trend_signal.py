"""First-party trend signals, with a k-anonymity floor (Phase 11).

WHY FIRST-PARTY AND NOT A TREND FEED
-------------------------------------
Phase 11 says trends come "on licensed or first-party sources only, capped at
<=10% of score". There is no licensed feed, and scraping one is both a legal
problem and a quality one — a runway trend has no bearing on what is in this
wardrobe. So the signal is our own wear logs: which subcategories, colours,
patterns and materials are being worn MORE than their own recent baseline.

THE K-ANONYMITY FLOOR IS THE WHOLE DESIGN, NOT A REFINEMENT
------------------------------------------------------------
A trend aggregated across tenants is a cross-tenant read, and a "trend"
computed from two users is a report of what those two users wore this
fortnight. At n=1 it is literally the owner's own wardrobe fed back to them as
a trend, which is both useless and a privacy claim we should not make.

So a row is only published when at least MIN_COHORT_USERS distinct users
contributed wears to it. Below that the trend does not exist and
`trend_alignment` reports zero — the same "absent beats confidently wrong"
rule the validator and the VTON router follow.

`users_contributing` is stored rather than only checked, so the floor can be
audited after the fact instead of being taken on trust.

NOT TENANT-SCOPED, AND DELIBERATELY SO
---------------------------------------
This table is an aggregate over all tenants, so it carries no `user_id` and no
RLS policy. It is readable by every tenant because a published row is, by the
floor above, not attributable to any of them. The aggregation itself runs as
the owner in the nightly job, the same pattern as the `ops_*` functions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_trend_signal"
down_revision = "0014_deferred_waits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trend_signal",
        sa.Column("field_name", sa.String(32), nullable=False),
        sa.Column("field_value", sa.String(64), nullable=False),
        # 0..1, already clamped by the job. Kept in range here too so a bad
        # write cannot make the weighted sum lie about influence.
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column(
            "users_contributing",
            sa.Integer(),
            nullable=False,
            comment="Distinct tenants whose wears built this row. Stored so the "
            "k-anonymity floor is auditable rather than taken on trust.",
        ),
        sa.Column("wears_recent", sa.Integer(), nullable=False),
        sa.Column("wears_baseline", sa.Integer(), nullable=False),
        sa.Column(
            "computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("field_name", "field_value"),
        sa.CheckConstraint("score >= 0 AND score <= 1", name="ck_trend_score_range"),
        comment="Cross-tenant wear-velocity trends. No user_id and no RLS: a "
        "published row passed the k-anonymity floor and is not attributable.",
    )

    # The aggregation. Returns candidates with their TRUE cohort size,
    # including rows below the floor -- the floor is applied by the caller so
    # "suppressed by k-anonymity" and "no data at all" stay distinguishable in
    # the log. A job that cannot tell those apart is how the RLS bug above
    # stayed invisible.
    op.execute("""
        CREATE OR REPLACE FUNCTION trend_candidates(
            recent_days integer, baseline_days integer, min_recent integer
        )
        RETURNS TABLE (
            field_name text, field_value text,
            wears_recent bigint, wears_baseline bigint, users bigint
        )
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
            WITH w AS (
                SELECT wl.user_id, wl.worn_on,
                       g.subcategory::text AS subcategory,
                       g.primary_colour::text AS primary_colour,
                       g.pattern::text AS pattern,
                       g.material::text AS material
                FROM wear_log wl JOIN garments g ON g.id = wl.garment_id
                WHERE wl.worn_on > current_date - baseline_days
            ),
            unpivoted AS (
                SELECT user_id, worn_on, 'subcategory' AS f, subcategory AS v FROM w
                UNION ALL SELECT user_id, worn_on, 'primary_colour', primary_colour FROM w
                UNION ALL SELECT user_id, worn_on, 'pattern', pattern FROM w
                UNION ALL SELECT user_id, worn_on, 'material', material FROM w
            ),
            agg AS (
                SELECT f, v,
                       count(*) FILTER (
                           WHERE worn_on > current_date - recent_days) AS recent,
                       count(*) FILTER (
                           WHERE worn_on <= current_date - recent_days) AS baseline,
                       count(DISTINCT user_id) FILTER (
                           WHERE worn_on > current_date - recent_days) AS users
                FROM unpivoted WHERE v IS NOT NULL GROUP BY 1, 2
            )
            SELECT f, v, recent, baseline, users
            FROM agg WHERE recent >= min_recent
        $$
    """)
    # CREATE FUNCTION grants EXECUTE to PUBLIC by default, which would hand
    # every role the owner's read access. Same order as 0006.
    op.execute("REVOKE ALL ON FUNCTION trend_candidates(integer, integer, integer) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION trend_candidates(integer, integer, integer) TO stylist_app"
    )

    # Read by every tenant, written only by the nightly job running as owner.
    # SELECT-only for the app role: a tenant-facing request has no business
    # writing an aggregate over other tenants, and granting it would make the
    # k-anonymity floor bypassable from the request path.
    op.execute("GRANT SELECT ON trend_signal TO stylist_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS trend_candidates(integer, integer, integer)")
    op.drop_table("trend_signal")
