"""Cross-tenant operational aggregates, without handing the API superuser

Revision ID: 0006_ops_aggregates
Revises: 0005_wear_search_dedupe
Create Date: 2026-09-11

THE PROBLEM THIS SOLVES
-----------------------
Three of the four Phase 5 alerts — DLQ age, ingest success rate, spend spike —
are questions about ALL tenants. The API runs as `stylist_app`, which is
NOSUPERUSER / NOBYPASSRLS by design, and every tenant table is FORCE RLS. With
no tenant context those queries return zero rows.

Zero rows is not an error. `count(*) = 0` reads as "nothing is in the DLQ",
`max(age) = 0` reads as "nothing is stuck", and a success rate over no samples
reads as "nothing is failing". The alerts were live, green, and structurally
incapable of ever firing — the worst possible failure for monitoring, because
it is indistinguishable from health.

WHY FUNCTIONS AND NOT A SECOND CONNECTION
-----------------------------------------
The obvious fix is to give the API a second engine using the owner DSN. That
puts superuser credentials in the process that serves the internet, so a single
SQL-injection or SSRF bug escalates from one tenant's data to every tenant's
data plus DDL.

SECURITY DEFINER functions invert it: the function runs with the owner's
rights, the API keeps none, and the entire privileged surface is these seven
functions — each of which returns counts, rates and timestamps. There is no
argument that can make one of them return a garment, an image key or an email,
so injection into a parameter buys nothing.

`SET search_path = public, pg_temp` on every one: without it a caller can
create a same-named object in a schema earlier in their search_path and have
the definer execute it with the owner's privileges. That is the standard
SECURITY DEFINER escalation, and the mitigation is not optional.
"""

from __future__ import annotations

from alembic import op

revision = "0006_ops_aggregates"
down_revision = "0005_wear_search_dedupe"
branch_labels = None
depends_on = None

FUNCTIONS: dict[str, str] = {
    # ---- alert 1: DLQ age -------------------------------------------------
    "ops_dlq_stats()": """
        RETURNS TABLE (depth bigint, oldest_seconds double precision)
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT count(*)::bigint,
                   COALESCE(EXTRACT(EPOCH FROM (now() - min(dlq_at))), 0)::double precision
            FROM jobs WHERE dlq_at IS NOT NULL
        $$
    """,
    # ---- alert 2: spend spike --------------------------------------------
    "ops_spend_stats(trailing_days integer)": """
        RETURNS TABLE (today numeric, trailing_mean numeric, trailing_days_seen bigint)
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
            WITH daily AS (
                SELECT created_at::date AS day, sum(cost_usd) AS spend
                FROM model_calls
                WHERE created_at > now() - make_interval(days => trailing_days + 1)
                GROUP BY 1
            )
            SELECT COALESCE((SELECT spend FROM daily WHERE day = CURRENT_DATE), 0)::numeric,
                   COALESCE(AVG(spend) FILTER (WHERE day < CURRENT_DATE), 0)::numeric,
                   count(*) FILTER (WHERE day < CURRENT_DATE)::bigint
            FROM daily
        $$
    """,
    # ---- alert 3: ingest success rate ------------------------------------
    "ops_ingest_stats(window_hours integer)": """
        RETURNS TABLE (ok bigint, dead bigint, total bigint)
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT count(*) FILTER (WHERE dlq_at IS NULL AND state = 'complete')::bigint,
                   count(*) FILTER (WHERE dlq_at IS NOT NULL)::bigint,
                   count(*)::bigint
            FROM jobs
            WHERE created_at > now() - make_interval(hours => window_hours)
        $$
    """,
    # ---- dashboards -------------------------------------------------------
    "ops_funnel(window_hours integer)": """
        RETURNS TABLE (state varchar, n bigint)
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT j.state, count(*)::bigint
            FROM jobs j
            WHERE j.created_at > now() - make_interval(hours => window_hours)
            GROUP BY 1 ORDER BY 2 DESC
        $$
    """,
    "ops_latency(window_hours integer)": """
        RETURNS TABLE (p50 double precision, p95 double precision, n bigint)
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT percentile_disc(0.5) WITHIN GROUP (
                       ORDER BY EXTRACT(EPOCH FROM (updated_at - created_at)))::double precision,
                   percentile_disc(0.95) WITHIN GROUP (
                       ORDER BY EXTRACT(EPOCH FROM (updated_at - created_at)))::double precision,
                   count(*)::bigint
            FROM jobs
            WHERE state = 'complete'
              AND created_at > now() - make_interval(hours => window_hours)
        $$
    """,
    "ops_model_spend(window_hours integer)": """
        RETURNS TABLE (model_name varchar, calls bigint, usd numeric,
                       avg_ms integer, cached bigint)
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT m.model_name, count(*)::bigint, COALESCE(sum(m.cost_usd), 0)::numeric,
                   COALESCE(avg(m.latency_ms), 0)::integer,
                   count(*) FILTER (WHERE m.cache_hit)::bigint
            FROM model_calls m
            WHERE m.created_at > now() - make_interval(hours => window_hours)
            GROUP BY 1 ORDER BY 3 DESC
        $$
    """,
    "ops_correction_rate(window_days integer)": """
        RETURNS TABLE (field_name varchar, n bigint, avg_conf double precision,
                       garments bigint)
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT c.field_name, count(*)::bigint,
                   avg(c.model_confidence)::double precision,
                   (SELECT count(*) FROM garments
                     WHERE is_active AND state <> 'received')::bigint
            FROM garment_corrections c
            WHERE c.created_at > now() - make_interval(days => window_days)
            GROUP BY 1 ORDER BY 2 DESC
        $$
    """,
}


def upgrade() -> None:
    for signature, body in FUNCTIONS.items():
        op.execute(f"CREATE OR REPLACE FUNCTION {signature} {body}")
        name = signature.split("(")[0]
        # REVOKE from PUBLIC first: CREATE FUNCTION grants EXECUTE to PUBLIC by
        # default, which would hand every role the owner's read access.
        args = signature[signature.index("(") :]
        op.execute(f"REVOKE ALL ON FUNCTION {name}{args} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {name}{args} TO stylist_app")


def downgrade() -> None:
    for signature in FUNCTIONS:
        name = signature.split("(")[0]
        args = signature[signature.index("(") :]
        op.execute(f"DROP FUNCTION IF EXISTS {name}{args}")
