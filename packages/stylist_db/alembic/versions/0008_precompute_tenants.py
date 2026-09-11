"""Let the precompute driver enumerate tenants without bypassing RLS

Revision ID: 0008_precompute_tenants
Revises: 0007_outfits
Create Date: 2026-09-11

THE SAME TRAP AS 0006, IN A NEW PLACE
-------------------------------------
The nightly precompute has to iterate every tenant. It runs as `stylist_app`
(NOSUPERUSER / NOBYPASSRLS) and `garments` is FORCE RLS, so

    SELECT DISTINCT user_id FROM garments

returns ZERO ROWS with no error. The job then reports
`{"ran": true, "tenants": 0, "written": 0}` — a successful run that did
nothing, indistinguishable from a night when nobody had any garments. It would
have precomputed nothing forever and every `GET /suggestions` would have been
empty with no failure anywhere to point at.

This is the second instance of the identical mistake: the Phase 5 alerts
queried tenant tables the same way and were structurally incapable of firing.
Fixed the same way, for the same reason — a SECURITY DEFINER function, so the
application role gains exactly one capability instead of superuser.

WHAT THIS EXPOSES, EXACTLY
--------------------------
A list of user UUIDs that own at least one active garment. No email, no
garment, no image key, no count per user. RLS still prevents the app role from
reading a single row belonging to any of them — knowing an opaque id exists
does not grant access to what it owns. That is the minimum the driver needs and
nothing more.
"""

from __future__ import annotations

from alembic import op

revision = "0008_precompute_tenants"
down_revision = "0007_outfits"
branch_labels = None
depends_on = None

SIGNATURE = "precompute_tenants()"
BODY = """
    RETURNS TABLE (user_id uuid)
    LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
        SELECT DISTINCT g.user_id
        FROM garments g
        WHERE g.is_active
          AND g.state NOT IN ('rejected', 'quarantined')
    $$
"""


def upgrade() -> None:
    op.execute(f"CREATE OR REPLACE FUNCTION {SIGNATURE} {BODY}")
    # CREATE FUNCTION grants EXECUTE to PUBLIC by default, which would hand the
    # definer's read access to every role in the database.
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO stylist_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {SIGNATURE}")
