"""Rebuild every style vector from the event log (Phase 8).

WHY THIS EXISTS, AND WHY IT EXISTS NOW
---------------------------------------
The plan is unusually blunt about it: write this script **in this phase** and
test it, "or you will accumulate unrebuildable state". A style vector is an
EWMA folded in one event at a time; if the only copy is the running total, then
a bug in the fold, a changed alpha, or a batch of events that arrived while the
embedding was NULL are all permanent and undetectable. The log is the truth and
this is the proof that it is.

The Phase 8 exit criterion is that this reproduces live vectors EXACTLY. That
is achievable because `stylist_domain.style.apply_event` is pure and both
callers use it — the live handler and this script are one implementation
invoked twice, not two that must agree.

    python scripts/rebuild_style_vectors.py --check     # compare, write nothing
    python scripts/rebuild_style_vectors.py             # rebuild and persist
    python scripts/rebuild_style_vectors.py --user <id>

`--check` is the default posture for CI and the exit criterion: it exits 1 if
any tenant's replay disagrees with what is stored, and 0 when every one
matches. Writing is opt-in because "fix the divergence" and "tell me there is
one" are different operations and the first destroys the evidence for the
second.

ORDER IS THE WHOLE GAME
-----------------------
EWMA is not commutative. Events replay in `(created_at, id)` order, matching
`ix_feedback_replay`, because two events sharing a timestamp must still have
ONE canonical order — otherwise this script and the live vector differ by an
amount nobody can account for, and the criterion becomes unfalsifiable.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from typing import Any

from sqlalchemy import text

from stylist_db.session import dispose_engine, init_engine, system_session, tenant_session
from stylist_domain.style import (
    DEFAULT_ALPHA,
    StyleVector,
    apply_event,
    outfit_embedding,
    parse_embedding,
)

# Vectors are compared with a tolerance rather than by `==`.
#
# The live vector makes one round trip through Postgres per event (pgvector
# stores float4), while the replay holds float64 in memory throughout. Those
# differ in the last bits by construction, so bit equality would fail for a
# reason that is not a bug — and a criterion that cannot pass teaches everyone
# to ignore it. 1e-5 is far tighter than any real divergence: a missed event,
# a wrong sign or a changed alpha all move a unit vector by >= 1e-3.
TOLERANCE = 1e-5


def max_abs_diff(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    if len(a) != len(b):
        return float("inf")
    return max((abs(x - y) for x, y in zip(a, b, strict=True)), default=0.0)


async def _tenants_with_feedback(session: Any) -> list[uuid.UUID]:
    """Tenants to replay.

    Via the same SECURITY DEFINER path the precompute uses: this runs as
    `stylist_app` against FORCE-RLS tables, so a plain cross-tenant SELECT
    returns zero rows with NO ERROR — and the script would then report "0
    tenants, all consistent", which is indistinguishable from success. That
    exact failure has already happened twice in this project (the Phase 5 ops
    alerts, the Phase 6 precompute driver).
    """
    rows = await session.execute(text("SELECT user_id FROM feedback_tenants()"))
    return [r[0] for r in rows]


async def replay_tenant(user_id: uuid.UUID) -> tuple[StyleVector | None, int]:
    """Fold every event for one tenant, in replay order. Returns (vector, rows)."""
    async with tenant_session(user_id) as db:
        rows = await db.execute(
            text(
                """
                SELECT f.id::text AS id, f.kind::text AS kind, f.garment_ids
                FROM outfit_feedback f
                ORDER BY f.created_at, f.id
                """
            )
        )
        events = [dict(r) for r in rows.mappings()]

        # Embeddings are fetched ONCE for every garment mentioned, not per
        # event. A tenant with 5k events over a 400-item wardrobe would
        # otherwise issue 5k queries to read the same 400 rows.
        all_ids = sorted({str(g) for e in events for g in e["garment_ids"]})
        emb_rows = await db.execute(
            text(
                "SELECT id::text AS id, embedding FROM garments "
                "WHERE id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": all_ids},
        )
        embeddings = {r["id"]: parse_embedding(r["embedding"]) for r in emb_rows.mappings()}

    style: StyleVector | None = None
    for event in events:
        vecs = [
            embeddings[str(g)] for g in event["garment_ids"] if embeddings.get(str(g)) is not None
        ]
        outfit = outfit_embedding(vecs)
        if outfit is None:
            # The garments were deleted, or were never embedded. The event is
            # real and stays in the log; it simply moves nothing. Skipping it
            # SILENTLY would be wrong in the other direction — see the count
            # reported below.
            continue
        style = apply_event(
            style,
            kind=event["kind"],
            outfit_vec=outfit,
            event_id=event["id"],
            alpha=style.alpha if style else DEFAULT_ALPHA,
        )
    return style, len(events)


async def stored_vector(user_id: uuid.UUID) -> StyleVector | None:
    async with tenant_session(user_id) as db:
        row = await db.execute(
            text(
                "SELECT vector, events_applied, last_event_id::text AS last_event_id, alpha "
                "FROM user_style_vector LIMIT 1"
            )
        )
        r = row.mappings().one_or_none()
    if r is None:
        return None
    return StyleVector(
        vector=tuple(parse_embedding(r["vector"]) or ()),
        events_applied=int(r["events_applied"]),
        last_event_id=r["last_event_id"],
        alpha=float(r["alpha"]),
    )


async def persist(user_id: uuid.UUID, style: StyleVector) -> None:
    async with tenant_session(user_id) as db:
        await db.execute(
            text(
                """
                INSERT INTO user_style_vector
                    (user_id, vector, events_applied, last_event_id, alpha, updated_at)
                VALUES (:uid, :vec, :n, :last, :alpha, now())
                ON CONFLICT (user_id) DO UPDATE SET
                    vector = EXCLUDED.vector,
                    events_applied = EXCLUDED.events_applied,
                    last_event_id = EXCLUDED.last_event_id,
                    alpha = EXCLUDED.alpha,
                    updated_at = now()
                """
            ),
            {
                "uid": user_id,
                "vec": str(list(style.vector)),
                "n": style.events_applied,
                "last": style.last_event_id,
                "alpha": style.alpha,
            },
        )


async def run(*, check_only: bool, only_user: uuid.UUID | None) -> int:
    init_engine(os.environ["DATABASE_URL"])
    try:
        if only_user is not None:
            tenants = [only_user]
        else:
            async with system_session() as session:
                tenants = await _tenants_with_feedback(session)

        print(f"replaying {len(tenants)} tenant(s)")
        diverged: list[str] = []
        rebuilt = 0

        for user_id in tenants:
            replayed, event_count = await replay_tenant(user_id)
            stored = await stored_vector(user_id)

            if replayed is None:
                if stored is not None:
                    diverged.append(f"{user_id}: stored vector exists but replay produced none")
                continue

            if stored is None:
                status = "MISSING"
                delta = float("inf")
            else:
                delta = max_abs_diff(replayed.vector, stored.vector)
                status = "ok" if delta <= TOLERANCE else "DIVERGED"

            if status != "ok":
                diverged.append(
                    f"{user_id}: {status} (max abs diff {delta:.2e}, {event_count} events in log)"
                )

            if not check_only:
                await persist(user_id, replayed)
                rebuilt += 1

        if check_only:
            if diverged:
                print(f"\n{len(diverged)} tenant(s) disagree with the log:")
                for line in diverged:
                    print(f"  {line}")
                print("\nRun without --check to rebuild from the log.")
                return 1
            print(f"all {len(tenants)} tenant(s) match the event log (tol {TOLERANCE:g})")
            return 0

        print(f"rebuilt {rebuilt} tenant(s)")
        if diverged:
            # Reported even when writing, because "we fixed 3 divergences" and
            # "nothing was wrong" are different facts and the run should not
            # conceal which one happened.
            print(f"  ({len(diverged)} had diverged before this run)")
        return 0
    finally:
        await dispose_engine()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare only; exit 1 on divergence and write nothing",
    )
    parser.add_argument("--user", type=uuid.UUID, default=None, help="one tenant")
    args = parser.parse_args()
    return asyncio.run(run(check_only=args.check, only_user=args.user))


if __name__ == "__main__":
    sys.exit(main())
