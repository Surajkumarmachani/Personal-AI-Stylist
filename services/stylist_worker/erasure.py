"""The erasure saga: §C5's seven steps, resumable (Phase 9).

WHY A SAGA AND NOT A TRANSACTION
---------------------------------
A user's data is in Postgres, in object storage with VERSIONING ON, in a model
provider's cache, in a Google Calendar grant, in Firebase's device registry and
— when enabled — in traces. No transaction spans those. So each step is
separately durable, separately retryable, and recorded as it completes.

EVERY STEP IS IDEMPOTENT, BECAUSE EVERY STEP WILL BE RETRIED
--------------------------------------------------------------
A saga that cannot be re-run from the middle is a saga that fails permanently
the first time a provider is down. Each step here either checks what it already
did or uses an operation that is safe to repeat, and completed steps are
recorded so a resume SKIPS them — re-purging S3 versions is slow, and
re-revoking a token that is already gone reads as a failure when it is the
desired state.

WHAT WE CANNOT ERASE IS RECORDED AND DISCLOSED
------------------------------------------------
§C5: "record what could NOT be purged (and disclose it)". A provider with a
30-day retention window cannot be made to forget on demand, and neither can a
backup taken before the request. Reporting success while someone else still
holds the data is worse than saying so — the user's next question is a
regulator's first question.

ORDER IS NOT ARBITRARY
----------------------
Rows are deleted LAST. Every earlier step needs them: the object keys live in
`garments`, the provider key in `user_profile`, the calendar token in
`calendar_link`. Deleting rows first would strand the objects permanently —
unreferenced, unerasable, and invisible to the next audit.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from stylist_db.session import system_session
from stylist_obs import stage_span

logger = logging.getLogger(__name__)

# §C5's order. `requested` is the entry state; `confirmed` is terminal success.
STEP_ORDER = (
    "soft_deleted",
    "provider_purged",
    "traces_scrubbed",
    "objects_deleted",
    "cdn_purged",
    "rows_deleted",
    "confirmed",
)

# The delete order and batching now live in `erase_user_rows()` (migration
# 0012), not here. Two of these tables are APPEND-ONLY and `stylist_app` cannot
# delete from them at all, so the order has to be inside the privileged
# function anyway — keeping a second copy in Python would be a list that drifts
# from the one that actually runs.
#
# `audit_log` is absent from that function on purpose: step 7 writes the proof
# the erasure happened, and a deletion record you delete proves nothing.


async def request_erasure(user_id: uuid.UUID, email: str | None) -> dict[str, Any]:
    """Record the request and complete step 1 SYNCHRONOUSLY.

    Step 1 is user-visible — auth revoked, API 410 — and must happen inside the
    request that asked for it. A user who taps "delete my account" and can
    still log in a second later does not believe anything else the saga claims.
    Steps 2-7 run in the background against a 30-day SLA.
    """
    digest = hashlib.sha256(email.encode()).hexdigest() if email else None

    async with system_session() as db:
        existing = await db.execute(
            text(
                "SELECT id, state FROM erasure_request "
                "WHERE user_id = :u AND state NOT IN ('confirmed', 'failed')"
            ),
            {"u": user_id},
        )
        row = existing.mappings().one_or_none()
        if row is not None:
            # Idempotent. A second DELETE /me must not start a competing saga
            # racing the first over the same rows.
            return {"erasure_id": str(row["id"]), "state": row["state"], "already_requested": True}

        erasure_id = uuid.uuid4()
        await db.execute(
            text(
                "INSERT INTO erasure_request (id, user_id, email_sha256, state) "
                "VALUES (:i, :u, :e, 'requested')"
            ),
            {"i": erasure_id, "u": user_id, "e": digest},
        )
        await _soft_delete(db, user_id)
        await _advance(db, erasure_id, "soft_deleted", counts={})

    return {"erasure_id": str(erasure_id), "state": "soft_deleted", "already_requested": False}


async def _soft_delete(db: Any, user_id: uuid.UUID) -> None:
    """Step 1. The account becomes unusable NOW.

    `deleted_at` rather than a row delete: steps 2-6 still need this user's
    rows to find the objects, tokens and keys they have to purge. Deleting the
    anchor first strands everything downstream of it.
    """
    await db.execute(
        text("UPDATE users SET deleted_at = now() WHERE id = :u AND deleted_at IS NULL"),
        {"u": user_id},
    )


async def _advance(
    db: Any,
    erasure_id: uuid.UUID,
    step: str,
    *,
    counts: dict[str, Any],
    unpurgeable: list[dict[str, str]] | None = None,
) -> None:
    """Record a completed step. Resumption reads this, so it is the only thing
    standing between a retry and a re-run of work already done."""
    await db.execute(
        text(
            """
            UPDATE erasure_request SET
              -- EVERY reference to :step is cast. asyncpg deduces a parameter's
              -- type from its use, and this one appears in three contexts
              -- (assignment, `= ANY(varchar[])`, comparison to a literal), which
              -- it reports as `AmbiguousParameterError: inconsistent types
              -- deduced for parameter $1` — three frames from anything that
              -- names the column.
              state = CAST(:step AS varchar),
              completed_steps = CASE
                WHEN CAST(:step AS varchar) = ANY(completed_steps) THEN completed_steps
                ELSE array_append(completed_steps, CAST(:step AS varchar))
              END,
              counts = counts || CAST(:counts AS jsonb),
              unpurgeable = CASE
                WHEN CAST(:unpurgeable AS jsonb) = '[]'::jsonb THEN unpurgeable
                ELSE unpurgeable || CAST(:unpurgeable AS jsonb)
              END,
              updated_at = now(),
              completed_at = CASE WHEN CAST(:step AS varchar) = 'confirmed'
                                  THEN now() ELSE completed_at END
            WHERE id = :i
            """
        ),
        {
            "i": erasure_id,
            "step": step,
            "counts": _json(counts),
            "unpurgeable": _json(unpurgeable or []),
        },
    )


def _json(value: Any) -> str:
    import json

    return json.dumps(value)


# ------------------------------------------------------------ the steps


async def _purge_providers(
    db: Any, user_id: uuid.UUID
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Step 2. Revoke everything a third party holds on this user's behalf.

    Three grants exist in this system, and each fails differently:
      - the LiteLLM virtual key (ours to delete)
      - the Google Calendar refresh token (revocable at Google, and we record
        whether Google CONFIRMED it — see calendar_link.revoked_at_provider)
      - Firebase device tokens (disabled locally; FCM expires them itself)

    The model PROVIDER's own cache is the one we cannot reach. Gemini has a
    retention window and no per-user purge API, so it is disclosed rather than
    claimed.
    """
    counts: dict[str, Any] = {}
    unpurgeable: list[dict[str, str]] = []

    row = await db.execute(
        text("SELECT litellm_key FROM user_profile WHERE user_id = :u"), {"u": user_id}
    )
    key = row.scalar_one_or_none()
    if key:
        from stylist_worker import deps

        try:
            await deps.get_litellm_client().delete_virtual_key(key)
            counts["litellm_key_revoked"] = 1
        except Exception as exc:
            logger.warning("erasure: could not revoke virtual key: %s", exc)
            unpurgeable.append(
                {
                    "system": "litellm",
                    "reason": f"virtual key revocation failed: {type(exc).__name__}",
                }
            )

    cal = await db.execute(
        text("SELECT refresh_token FROM calendar_link WHERE user_id = :u"), {"u": user_id}
    )
    token = cal.scalar_one_or_none()
    if token:
        from stylist_clients import google_calendar as gcal

        confirmed = await gcal.revoke(token)
        counts["calendar_revoked"] = 1
        await db.execute(
            text(
                "UPDATE calendar_link SET refresh_token = NULL, revoked_at = now(), "
                "revoked_at_provider = :c WHERE user_id = :u"
            ),
            {"u": user_id, "c": confirmed},
        )
        if not confirmed:
            # A local clear Google did not confirm is a DIFFERENT state, and
            # the difference is exactly what an erasure request is asking
            # about: not "did we forget", but "can they still read it".
            unpurgeable.append(
                {"system": "google_calendar", "reason": "revocation not confirmed by Google"}
            )

    devices = await db.execute(
        text(
            "UPDATE device_token SET disabled_at = now(), disabled_reason = 'erasure', "
            "token = '' WHERE user_id = :u AND disabled_at IS NULL RETURNING id"
        ),
        {"u": user_id},
    )
    counts["devices_disabled"] = len(list(devices))

    # Always disclosed. No provider in this stack offers per-user cache purge,
    # and claiming otherwise would be the one lie this record exists to prevent.
    calls = await db.execute(
        text("SELECT count(*) FROM model_calls WHERE user_id = :u"), {"u": user_id}
    )
    if int(calls.scalar_one() or 0):
        unpurgeable.append(
            {
                "system": "model_provider",
                "reason": "prompts and images sent for tagging may persist in the "
                "provider's retention window; no per-user purge API is offered",
            }
        )

    return counts, unpurgeable


async def _scrub_traces() -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Step 3. Nothing to scrub in this deployment, and it says so.

    Langfuse and the OTel exporter are both opt-in and unset by default
    (Phase 5). Returning "0 spans scrubbed" would be indistinguishable from a
    scrub that silently failed, so the step records that tracing was OFF —
    which is the fact a later audit actually needs.
    """
    import os

    from stylist_api.settings import get_settings

    tracing_on = bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")) or bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY")
    )
    get_settings()  # touched so a misconfiguration surfaces here, not later
    if not tracing_on:
        return {"traces": "tracing_disabled"}, []
    return {"traces": "scrub_required"}, [
        {
            "system": "traces",
            "reason": "tracing is enabled but no automated span scrub is implemented; "
            "delete or pseudonymise spans manually before confirming",
        }
    ]


async def _delete_objects(
    db: Any, user_id: uuid.UUID
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Step 4. EVERY VERSION, not just the current one.

    §C5 is emphatic and it is the single most common way "we deleted it" turns
    out to be false: the bucket has versioning ON, so `delete_object` writes a
    delete MARKER and leaves the bytes recoverable. That is not erasure.
    """
    from stylist_worker import deps

    store = deps.get_object_store()
    prefixes = [
        f"originals/{user_id}/",
        f"cutouts/{user_id}/",
        f"masks/{user_id}/",
        f"boards/{user_id}/",
        f"grids/{user_id}/",
    ]

    deleted = 0
    for prefix in prefixes:
        # RAISES, deliberately. The first version caught this and recorded it as
        # `unpurgeable`, which would confirm an erasure while the objects were
        # still in the bucket — the single worst outcome this saga can produce,
        # because it is indistinguishable from success in every record we keep.
        #
        # "Storage was down" is a TRANSIENT failure and belongs in the retry
        # path; `unpurgeable` is for data we genuinely cannot reach, like a
        # provider's own cache. Conflating them turns a retryable outage into a
        # permanent, documented lie.
        deleted += store.delete_all_versions(prefix)

    return {"objects_deleted": deleted}, []


async def _purge_cdn() -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Step 5. There is no CDN in this stack, and that is recorded as a FACT.

    Objects are served by presigned URLs straight from storage, so deleting the
    object is the purge. When a CDN is added this step gains a real
    implementation and a probe — and until then, "not applicable" is a more
    useful audit entry than a silent skip that looks identical to a no-op.
    """
    return {"cdn": "not_deployed"}, []


async def _delete_rows(db: Any, user_id: uuid.UUID) -> dict[str, Any]:
    """Step 6. Through `erase_user_rows()`, not raw DELETEs.

    LAST, because every earlier step reads these rows to find what to purge
    elsewhere. Deleting them first would strand the objects permanently —
    unreferenced, unerasable, invisible to the next audit.

    It goes through a SECURITY DEFINER function because two of these tables are
    APPEND-ONLY: `outfit_feedback` (0009) and `push_send` (0011) have DELETE
    revoked from `stylist_app`, since they are the logs every derived thing
    replays. That guarantee is right and it collides with Art. 17. Rather than
    granting DELETE back — which would dissolve it for every code path to serve
    one — erasure is the single privileged exception, named and auditable.

    The function returns per-table counts, which is what "verified absent" is
    checked against rather than asserted.
    """
    row = await db.execute(text("SELECT erase_user_rows(CAST(:u AS uuid))"), {"u": str(user_id)})
    counts = dict(row.scalar_one() or {})
    # `processed_keys` is not user-scoped and not personal data — see the
    # function in migration 0012. Reported so the gap is visible in the record
    # rather than being an absence nobody can account for later.
    counts["processed_keys"] = "not_user_scoped"
    return counts


async def _confirm(
    db: Any, erasure_id: uuid.UUID, user_id: uuid.UUID, counts: dict[str, Any]
) -> None:
    """Step 7. The immutable record that this happened.

    Written to `audit_log`, which step 6 deliberately does not delete. It holds
    the pseudonymous user id, timestamps and per-step counts — no name, no
    email, no object key. Both regimes require demonstrating that a request was
    honoured, and a deletion record you delete demonstrates nothing.
    """
    await db.execute(
        text(
            "INSERT INTO audit_log (id, user_id, action, subject_type, subject_id, detail) "
            "VALUES (:i, NULL, 'erasure.completed', 'erasure_request', :e, CAST(:d AS jsonb))"
        ),
        {"i": uuid.uuid4(), "e": erasure_id, "d": _json({"user_id": str(user_id), **counts})},
    )


# ------------------------------------------------------------ the driver


async def run_erasure(erasure_id: uuid.UUID, user_id: uuid.UUID, completed: list[str]) -> str:
    """Run the remaining steps for one request. Resumable.

    Steps already in `completed` are SKIPPED, not re-run. That is what makes a
    retry cheap and what stops a resumed saga reporting failures for work that
    already succeeded.
    """
    done = set(completed)
    failure: str | None = None

    async with system_session() as db:
        # SET THE TENANT CONTEXT, or step 2 silently does nothing.
        #
        # `user_profile`, `calendar_link` and `device_token` are all FORCE ROW
        # LEVEL SECURITY. Read from a system session with no `app.user_id`,
        # every one returns ZERO ROWS WITH NO ERROR — so the provider purge
        # found no virtual key, no calendar token and no devices, reported
        # success, and left all three live. A test caught it; nothing in
        # production would have.
        #
        # This is the sixth instance of that trap in this codebase. The others
        # are solved with SECURITY DEFINER functions because they are
        # CROSS-tenant reads; this one is single-tenant, so setting the context
        # is both simpler and more precise — the saga can only ever touch the
        # user it was asked about.
        #
        # The user is SOFT-deleted at this point, not gone, so the context is
        # still valid. Step 6 removes the row, which is why it runs last.
        await db.execute(
            text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(user_id)}
        )
        try:
            if "provider_purged" not in done:
                counts, unpurgeable = await _purge_providers(db, user_id)
                await _advance(
                    db, erasure_id, "provider_purged", counts=counts, unpurgeable=unpurgeable
                )

            if "traces_scrubbed" not in done:
                counts, unpurgeable = await _scrub_traces()
                await _advance(
                    db, erasure_id, "traces_scrubbed", counts=counts, unpurgeable=unpurgeable
                )

            if "objects_deleted" not in done:
                counts, unpurgeable = await _delete_objects(db, user_id)
                await _advance(
                    db, erasure_id, "objects_deleted", counts=counts, unpurgeable=unpurgeable
                )

            if "cdn_purged" not in done:
                counts, unpurgeable = await _purge_cdn()
                await _advance(db, erasure_id, "cdn_purged", counts=counts, unpurgeable=unpurgeable)

            if "rows_deleted" not in done:
                counts = await _delete_rows(db, user_id)
                await _advance(db, erasure_id, "rows_deleted", counts={"rows": counts})

            if "confirmed" not in done:
                row = await db.execute(
                    text("SELECT counts FROM erasure_request WHERE id = :i"), {"i": erasure_id}
                )
                await _confirm(db, erasure_id, user_id, row.scalar_one() or {})
                await _advance(db, erasure_id, "confirmed", counts={})

        except Exception as exc:
            logger.exception("erasure %s failed mid-saga", erasure_id)
            failure = f"{type(exc).__name__}: {exc}"[:2000]

    if failure is not None:
        # RECORDED IN A FRESH SESSION, outside the `except`.
        #
        # A failed statement aborts the whole Postgres transaction, so every
        # subsequent command in it raises `InFailedSQLTransactionError` — the
        # handler's own UPDATE included. Writing the error inside the session
        # that just failed therefore fails too, AND masks the original
        # exception with a confusing one about transaction state, which is
        # exactly what happened the first time this ran.
        async with system_session() as db:
            # NOT marked `failed` — that state is terminal and would abandon
            # the request. An erasure that hit a transient error must keep
            # retrying until the SLA, because the deadline is legal rather
            # than operational.
            await db.execute(
                text(
                    "UPDATE erasure_request SET last_error = :e, attempts = attempts + 1, "
                    "updated_at = now() WHERE id = :i"
                ),
                {"i": erasure_id, "e": failure},
            )
        return "retry"
    return "confirmed"


async def drain_erasures(ctx: dict[str, Any]) -> dict[str, Any]:
    """Cron entry point. Resumes every open request.

    §C5: "alert at 7 days, page at 25 days" against a 30-day SLA. The overdue
    counts are returned so `/ops/alerts` can read them rather than this job
    deciding on its own what is worth waking someone for.
    """
    async with system_session() as db:
        rows = await db.execute(text("SELECT * FROM pending_erasures()"))
        pending = [dict(r) for r in rows.mappings()]

    confirmed = retried = 0
    with stage_span("drain_erasures"):
        for req in pending:
            outcome = await run_erasure(
                req["id"], req["user_id"], list(req["completed_steps"] or [])
            )
            if outcome == "confirmed":
                confirmed += 1
            else:
                retried += 1

    now = datetime.now(UTC)
    overdue = sum(1 for r in pending if r["sla_deadline"] < now)
    if overdue:
        # Loud, because this is the one deadline in the product with a
        # regulator behind it rather than a user.
        logger.error("erasure SLA BREACHED for %d request(s)", overdue)

    return {
        "pending": len(pending),
        "confirmed": confirmed,
        "retried": retried,
        "sla_breached": overdue,
    }
