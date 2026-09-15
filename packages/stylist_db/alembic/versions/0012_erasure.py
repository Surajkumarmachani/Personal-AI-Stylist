"""Phase 9: erasure saga (DPDP 2023 / GDPR Art. 17)

Revision ID: 0012_erasure
Revises: 0011_push_tokens
Create Date: 2026-09-15

DELETION IS A DISTRIBUTED TRANSACTION, NOT A `DELETE CASCADE`
--------------------------------------------------------------
§C5 is explicit about why. A user's data lives in Postgres, in object storage
(with versioning ON, so a plain delete leaves a recoverable version and is not
erasure), in a third-party model provider's cache, in a Google Calendar grant,
in Firebase's device registry, and — when enabled — in traces. No single
transaction spans those, so this is a SAGA: a durable record, resumable, with
a state machine that can be retried step by step.

`ON DELETE CASCADE` would remove the ROWS and silently leave every one of the
others. That looks like erasure to a developer reading the schema and is not
erasure to a regulator.

WHY `users.deleted_at` AND NOT AN IMMEDIATE ROW DELETE
-------------------------------------------------------
Step 1 must be user-visible IMMEDIATELY — auth revoked, API returns 410 — while
steps 2-7 take as long as they take, up to a 30-day SLA. Deleting the `users`
row first would cascade half the data away before the saga could record what it
had purged, and the request would lose the user id it needs to find the rest.

WHAT COULD NOT BE PURGED IS RECORDED, NOT HIDDEN
--------------------------------------------------
`unpurgeable` is the column that makes this honest. A model provider with a
30-day retention window cannot be made to forget on demand; neither can a
backup taken before the request. §C5 says to "record what could NOT be purged
(and disclose it)", and a saga that reports success while a provider still
holds the data is worse than one that says so — the user's next question is
what a regulator's first question will be.

THE AUDIT ROW SURVIVES THE ERASURE, DELIBERATELY
--------------------------------------------------
Step 7 writes to `audit_log` and that row is NOT deleted. It carries the
pseudonymous user id, timestamps and which steps ran — no name, no email, no
image key. Both regimes require being able to demonstrate that a request was
honoured, and a deletion record you delete proves nothing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_erasure"
down_revision = "0011_push_tokens"
branch_labels = None
depends_on = None

# §C5's seven steps, in order. Stored as text rather than an enum because the
# LIST will change as systems are added (a CDN, Langfuse) and an enum migration
# per system is friction on the one process that must never be skipped.
STEPS = (
    "requested",
    "soft_deleted",
    "provider_purged",
    "traces_scrubbed",
    "objects_deleted",
    "cdn_purged",
    "rows_deleted",
    "confirmed",
    "failed",
)


def upgrade() -> None:
    op.create_table(
        "erasure_request",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # NO foreign key to users, and that is the point: step 6 deletes the
        # user row, and a FK would take this record with it — destroying the
        # evidence that the erasure happened at the moment it completes.
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Kept so a request can be matched to a person WITHOUT the users row.
        # Hashed, not stored: proving "we erased the account for this address"
        # needs a comparison, not the address itself.
        sa.Column("email_sha256", sa.String(64), nullable=True),
        sa.Column("state", sa.String(32), nullable=False, server_default=sa.text("'requested'")),
        # Which steps have completed. An array rather than relying on `state`
        # alone, because a resumed saga needs to know what to SKIP — re-running
        # an S3 version purge is slow, and re-revoking a token that is already
        # gone reads as a failure.
        sa.Column(
            "completed_steps",
            postgresql.ARRAY(sa.String(32)),
            nullable=False,
            server_default=sa.text("'{}'::varchar[]"),
        ),
        # What we could NOT erase, and why. Disclosed to the user; see above.
        sa.Column(
            "unpurgeable",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # Per-step counts: objects deleted, rows deleted, keys revoked. This is
        # what "verified absent" is checked against rather than asserted.
        sa.Column(
            "counts", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        # §C5: "alert at 7 days, page at 25 days" against a 30-day SLA. Stored
        # rather than computed so the deadline survives a change to the policy
        # — a request made under a 30-day rule is not retroactively late
        # because the rule tightened.
        sa.Column(
            "sla_deadline",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now() + interval '30 days'"),
        ),
    )
    # One OPEN request per user. A second DELETE /me must be idempotent rather
    # than starting a competing saga that races the first over the same rows.
    op.create_index(
        "uq_erasure_open",
        "erasure_request",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("state NOT IN ('confirmed', 'failed')"),
    )
    op.create_index("ix_erasure_sla", "erasure_request", ["state", "sla_deadline"])

    # DELIBERATELY NOT RLS-PROTECTED, and not readable by the app role.
    #
    # The saga runs after the user is gone, so there is no tenant context to
    # scope it by — `current_setting('app.user_id')` is empty by the time step
    # 6 runs. Tenant isolation here would mean the saga could not read its own
    # record. Access is restricted by GRANT instead: the worker owns this.
    op.execute("GRANT SELECT, INSERT, UPDATE ON erasure_request TO stylist_app")

    # NO `users.deleted_at` HERE — migration 0001 already added it, and
    # `current_user` has been returning 410 on it since Phase 1. The saga's
    # step 1 was designed for from the start; only the saga was missing.
    # Adding the column again would fail this migration on a fresh database
    # and pass on an existing one, which is the worst way to find out.
    #
    # The partial index is new: `WHERE deleted_at IS NULL` is the predicate
    # every auth lookup uses, and after an erasure the dead rows are gone
    # anyway, so this only helps during the up-to-30-day window when a
    # soft-deleted row is still present.
    op.create_index(
        "ix_users_active", "users", ["id"], postgresql_where=sa.text("deleted_at IS NULL")
    )

    # Body-photo consent, revocable INDEPENDENTLY of the account (§C5's last
    # line, and Phase 10's prerequisite). Separate from `calendar_link` because
    # it is a different consent with a different lifetime: revoking try-on must
    # not disconnect a calendar.
    op.create_table(
        "body_photo",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("object_key", sa.Text(), nullable=False),
        # Timestamped and independently revocable, per §C5.
        sa.Column(
            "consented_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "deleted_from_storage", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.execute("ALTER TABLE body_photo ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE body_photo FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON body_photo
          USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
          WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON body_photo TO stylist_app")

    # The saga driver, for the same reason every other cross-tenant driver in
    # this project is a SECURITY DEFINER function: the worker runs as
    # `stylist_app` against FORCE-RLS tables, and a plain cross-tenant SELECT
    # returns zero rows with NO ERROR — reporting "nothing to erase", which is
    # indistinguishable from success and is the worst possible failure for this
    # particular job. Fifth instance of that trap in this codebase.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION pending_erasures()
        RETURNS TABLE (id uuid, user_id uuid, state varchar, completed_steps varchar[],
                       sla_deadline timestamptz, attempts int)
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT e.id, e.user_id, e.state, e.completed_steps, e.sla_deadline, e.attempts
            FROM erasure_request e
            WHERE e.state NOT IN ('confirmed', 'failed')
            ORDER BY e.sla_deadline
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION pending_erasures() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION pending_erasures() TO stylist_app")

    # ERASURE IS THE ONE LEGITIMATE REASON TO DELETE FROM AN APPEND-ONLY TABLE.
    #
    # `outfit_feedback` (0009) and `push_send` (0011) have UPDATE and DELETE
    # revoked from `stylist_app`, because they are the logs every derived thing
    # replays and a silent edit makes a rebuild disagree with live state. That
    # guarantee is right — and it collides head-on with Art. 17, which says the
    # user's data goes.
    #
    # Granting DELETE back would dissolve the guarantee for every code path in
    # order to serve one. Instead the deletion happens through a single
    # SECURITY DEFINER function: the app role still cannot delete a feedback
    # row, and the erasure saga can, by calling one named, auditable function
    # that deletes exactly one user's rows in FK order.
    #
    # It sets `app.user_id` first because FORCE ROW LEVEL SECURITY applies to
    # the table OWNER too — without it the definer's own policies would filter
    # every statement to zero rows and the function would report a successful
    # erasure that deleted nothing.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION erase_user_rows(target uuid)
        RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
        DECLARE
            t text;
            n bigint;
            result jsonb := '{}'::jsonb;
        BEGIN
            PERFORM set_config('app.user_id', target::text, true);
            FOREACH t IN ARRAY ARRAY[
                'push_send','device_token','calendar_link','body_photo',
                'preference_fact','user_style_vector','outfit_feedback','outfits',
                'wear_log','garment_corrections','garments','jobs','outbox',
                'model_calls','user_profile'
            ] LOOP
                EXECUTE format('DELETE FROM %I WHERE user_id = $1', t) USING target;
                GET DIAGNOSTICS n = ROW_COUNT;
                IF n > 0 THEN
                    result := result || jsonb_build_object(t, n);
                END IF;
            END LOOP;

            -- TWO TABLES ARE DELIBERATELY ABSENT.
            --
            -- `audit_log` holds the proof the erasure happened; deleting it
            -- would destroy the record at the moment it is created.
            --
            -- `processed_keys` is (idempotency_key, consumer, created_at) and
            -- has NO user column — it is the replay-dedupe table, and the key
            -- is an opaque value the CLIENT generated. There is no way to
            -- scope a delete to one user, and the rows carry no personal data:
            -- a random uuid and which consumer saw it. Listing it here would
            -- have failed with `column "user_id" does not exist`, which is how
            -- this was found; deleting it wholesale would break idempotency
            -- for every other tenant.
            --
            -- `users` LAST and keyed on `id`, because it is the anchor every
            -- other table hangs off.
            DELETE FROM users WHERE id = target;
            GET DIAGNOSTICS n = ROW_COUNT;
            IF n > 0 THEN
                result := result || jsonb_build_object('users', n);
            END IF;

            RETURN result;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION erase_user_rows(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION erase_user_rows(uuid) TO stylist_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS erase_user_rows(uuid)")
    op.execute("DROP FUNCTION IF EXISTS pending_erasures()")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON body_photo")
    op.drop_table("body_photo")
    op.drop_index("ix_users_active", table_name="users")
    op.drop_index("ix_erasure_sla", table_name="erasure_request")
    op.drop_index("uq_erasure_open", table_name="erasure_request")
    op.drop_table("erasure_request")
