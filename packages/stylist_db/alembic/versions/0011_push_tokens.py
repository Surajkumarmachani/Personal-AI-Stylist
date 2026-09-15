"""Phase 8: push device tokens and a per-user send log

Revision ID: 0011_push_tokens
Revises: 0010_calendar_link
Create Date: 2026-09-15

ONE ROW PER DEVICE, NOT ONE PER USER
-------------------------------------
A phone and a tablet are two registrations, and a user who reinstalls gets a
new FCM token while the old one keeps existing until FCM garbage-collects it.
Storing a single token per user means the notification follows whichever device
registered last, which is not the behaviour anyone expects and is impossible to
debug from the outside.

FCM TOKENS EXPIRE SILENTLY, SO THE TABLE RECORDS WHY ONE STOPPED
-----------------------------------------------------------------
A send to a stale token returns `UNREGISTERED` and FCM expects the sender to
delete it. If that verdict is not recorded, the nightly job retries the same
dead token every morning forever — quota spent on a device that no longer
exists. `disabled_at` and `disabled_reason` make a dead registration a fact
rather than a mystery, and `failure_count` catches the case where a token is
failing for some other reason and should be looked at rather than dropped.

THE SEND LOG EXISTS TO STOP DOUBLE-SENDING
-------------------------------------------
`push_send` has a unique index on `(user_id, kind, sent_on)`. The daily digest
fires from a cron that can run on several replicas and can be retried; without
a uniqueness constraint in the DATABASE, a retry after a partial failure sends
a second 07:00 notification. Nobody forgives an app that notifies twice.

It is a log rather than a counter for the same reason `wear_log` is: "did we
send today" and "when did we last send" are different questions, and only rows
answer both.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_push_tokens"
down_revision = "0010_calendar_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_token",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The FCM registration token. Not a secret in the way a private key is
        # — it authorises sending TO this device, not acting AS anyone — but it
        # identifies a device and is treated as personal data under erasure.
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        # IANA zone for THIS device. The plan says "07:00 local", and local is
        # a property of where the phone is, not of the account — a user who
        # travels should not get their digest at 07:00 in the timezone they
        # signed up in. Falls back to user_profile.timezone when absent.
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        # Set when FCM says the token is dead, or the user turns push off.
        # The row is KEPT so "why did notifications stop" is answerable.
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_reason", sa.String(64), nullable=True),
    )
    # The same token can be re-registered by the app on every launch; that must
    # be an upsert, not a duplicate row.
    op.create_index("uq_device_token", "device_token", ["user_id", "token"], unique=True)
    op.create_index(
        "ix_device_token_active",
        "device_token",
        ["user_id"],
        postgresql_where=sa.text("disabled_at IS NULL"),
    )

    op.create_table(
        "push_send",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        # The user's LOCAL date, not UTC. A digest sent at 07:00 IST on the 5th
        # is 01:30 UTC on the 5th, but at 07:00 in Honolulu it is the 5th UTC
        # while still the 4th locally — keying on the UTC date would let one
        # user get two digests in a local day and another get none.
        sa.Column("sent_on", sa.Date(), nullable=False),
        sa.Column("devices_targeted", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("devices_delivered", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "sent_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # THE DOUBLE-SEND GUARD, in the database rather than in the job. A cron
    # that runs on three replicas and retries on failure will attempt this
    # twice; only a unique index makes the second attempt a no-op.
    op.create_index("uq_push_send_daily", "push_send", ["user_id", "kind", "sent_on"], unique=True)

    for table in ("device_token", "push_send"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
              USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
              WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
            """
        )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON device_token TO stylist_app")
    op.execute("GRANT SELECT, INSERT ON push_send TO stylist_app")
    # Append-only, enforced by REVOKE and not by a narrower GRANT: migration
    # 0001's ALTER DEFAULT PRIVILEGES already attached UPDATE and DELETE to
    # every future table, so a narrower grant reads like a restriction and does
    # nothing. Same trap as `outfit_feedback` in 0009.
    op.execute("REVOKE UPDATE, DELETE ON push_send FROM stylist_app")

    # The nightly sender needs the set of tenants with a live device before it
    # can set a tenant context, and as `stylist_app` against FORCE RLS a plain
    # cross-tenant SELECT returns zero rows with NO ERROR — reporting "nobody
    # to notify", which is indistinguishable from success. Fourth instance of
    # that trap in this project; same fix as `precompute_tenants()` (0008) and
    # `feedback_tenants()` (0009).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION push_tenants()
        RETURNS TABLE (user_id uuid)
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT DISTINCT d.user_id FROM device_token d WHERE d.disabled_at IS NULL
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION push_tenants() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION push_tenants() TO stylist_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS push_tenants()")
    for table in ("push_send", "device_token"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_index("uq_push_send_daily", table_name="push_send")
    op.drop_table("push_send")
    op.drop_index("ix_device_token_active", table_name="device_token")
    op.drop_index("uq_device_token", table_name="device_token")
    op.drop_table("device_token")
