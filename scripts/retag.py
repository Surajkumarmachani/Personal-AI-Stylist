"""Re-run tagging for garments whose tags came from the wrong model.

WHY THIS EXISTS
---------------
Tags are written ONCE, at ingest, by whatever the gateway was pointed at that
day. Changing `VLM_MODEL` does not touch a garment already in the wardrobe, so
a period spent on `vlm-tagger-mock` leaves permanent, confident, wrong tags —
and the mock answers the same handful of values for everything.

That is not cosmetic. Measured on this wardrobe: both `lower` garments were
mock-tagged `dress_code = festive_ethnic`, which made them incompatible with
every casual occasion. The suggester then had no usable `lower` at all and
fell back to the single `full_body` candidate, so ten garments produced one
outfit — jeans and shoes — and nothing explained why.

HOW IT WORKS: REWIND, DO NOT REIMPLEMENT
----------------------------------------
`run_pipeline` skips a stage whose `completed_state` is at or below the job's
current state, which is what makes a resumed job cheap. So rewinding the job
to the state just before `tag` and re-enqueuing makes the real pipeline re-run
tag, embed, dedupe and persist — the same code path as a fresh ingest, with no
second implementation of tagging to drift.

Nothing is re-validated, re-segmented or re-matted: those stages sit below the
rewind point and are skipped, so the cutouts you already have are untouched.

    python scripts/retag.py --email you@example.com --tagged-by vlm-tagger-mock
    python scripts/retag.py --email you@example.com --tagged-by vlm-tagger-mock --apply

Dry-run by default.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid

from sqlalchemy import text

from stylist_db.session import init_engine, system_session, tenant_session
from stylist_worker.state_machine import STATE_ORDER, IngestState

# Rewind target: the state a job is in immediately BEFORE the tag stage runs.
# Derived from STATE_ORDER rather than written as a literal, so inserting a
# stage between matte and tag cannot silently make this rewind too far.
REWIND_TO = STATE_ORDER[STATE_ORDER.index(IngestState.TAGGED) - 1]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument(
        "--tagged-by",
        default="vlm-tagger-mock",
        help="re-tag garments whose last tag call used this model",
    )
    ap.add_argument("--apply", action="store_true", help="enqueue; otherwise dry-run")
    args = ap.parse_args()

    init_engine(os.environ["DATABASE_URL"])

    async with system_session() as s:
        row = await s.execute(text("SELECT id FROM users WHERE email = :e"), {"e": args.email})
        found = row.scalar_one_or_none()
    if found is None:
        print(f"no such user: {args.email}")
        return 1
    user_id = uuid.UUID(str(found))

    # `model_calls` has NO RLS policy (relrowsecurity = f) — the same gap the
    # eval view documents — so the user_id predicate here is load-bearing, not
    # belt-and-braces. Without it this reads every tenant's calls.
    async with tenant_session(user_id) as s:
        rows = (
            (
                await s.execute(
                    text(
                        """
                        SELECT g.id AS garment_id, g.subcategory::text AS subcategory,
                               g.slot::text AS slot, g.dress_code::text AS dress_code,
                               j.id AS job_id, j.payload, j.state AS job_state
                        FROM garments g
                        JOIN jobs j ON j.garment_id = g.id AND j.kind = 'ingest'
                        WHERE g.is_active
                          AND g.state NOT IN ('rejected', 'quarantined')
                          AND (
                            SELECT mc.model_name
                            FROM model_calls mc
                            WHERE mc.job_id = j.id AND mc.purpose = 'tag'
                              AND mc.user_id = CAST(:uid AS uuid)
                            ORDER BY mc.created_at DESC LIMIT 1
                          ) = :model
                        ORDER BY g.created_at
                        """
                    ),
                    {"uid": str(user_id), "model": args.tagged_by},
                )
            )
            .mappings()
            .all()
        )

    if not rows:
        print(f"nothing tagged by {args.tagged_by!r} — nothing to do")
        return 0

    print(f"  garments last tagged by {args.tagged_by!r}:\n")
    for r in rows:
        print(
            f"    {str(r['garment_id'])[:8]}  {r['slot']:11} {r['subcategory']:14}"
            f" dress_code={r['dress_code']}"
        )
    print(f"\n  rewinding each job to {REWIND_TO} and re-running tag onward")
    if not args.apply:
        print(f"  {len(rows)} garment(s) would be re-tagged (dry run; pass --apply)")
        return 0

    # The same pool the relay uses, built the same way (worker/main.py builds
    # it from the queue DSN at startup; a standalone script has no ctx).
    from arq import create_pool
    from arq.connections import RedisSettings

    from stylist_api.settings import get_settings

    redis = await create_pool(RedisSettings.from_dsn(get_settings().redis_queue_url))
    for r in rows:
        async with tenant_session(user_id) as s:
            await s.execute(
                text(
                    """
                    UPDATE jobs
                    SET state = :state, last_error = NULL, updated_at = now()
                    WHERE id = :job_id
                    """
                ),
                {"state": str(REWIND_TO), "job_id": r["job_id"]},
            )
        # A distinct arq job id per retag round. Reusing `outbox-{id}` would be
        # deduped as already-completed and the job would never run — the same
        # trap the deferral path documents in worker/main.py.
        await redis.enqueue_job(
            "ingest_photo",
            user_id=str(user_id),
            aggregate_id=str(r["garment_id"]),
            payload={**dict(r["payload"]), "job_id": str(r["job_id"])},
            _job_id=f"retag-{r['job_id']}-{uuid.uuid4().hex[:8]}",
        )
        print(f"    queued {str(r['garment_id'])[:8]}")

    print(f"\n  {len(rows)} re-tag job(s) queued. Watch: scripts/dc logs -f worker")
    return 0


sys.exit(asyncio.run(main()))
