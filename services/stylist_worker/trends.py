"""Nightly first-party trend aggregation (Phase 11).

WHAT A "TREND" IS HERE
----------------------
Which garment attributes are being worn MORE than their own recent baseline,
across all tenants. Not a runway feed, not a scrape: Phase 11's constraint is
"licensed or first-party sources only", there is no licensed feed, and a runway
trend has no bearing on what is actually in these wardrobes.

Velocity against a value's OWN baseline, not share of total wears. Share would
make `t_shirt` permanently "trending" for being common, and the question is
what is RISING, not what is popular.

THE K-ANONYMITY FLOOR
---------------------
This is a cross-tenant aggregate, and a trend computed from two users is a
report of what those two users wore this fortnight. At n=1 it is the owner's
own wardrobe handed back as a trend — useless, and a privacy claim we should
not make.

So a row is published only when at least MIN_COHORT_USERS distinct tenants
contributed wears to it. Below the floor the trend does not exist and
`trend_alignment` scores zero, which follows the same rule as the validator and
the VTON router: an absent answer beats a confidently wrong one.

AGGREGATED BY A SECURITY DEFINER FUNCTION, AFTER GETTING THIS WRONG ONCE
------------------------------------------------------------------------
The first version of this job aggregated through `system_session()`. That
session has NO TENANT CONTEXT, and the session module's own docstring says
tenant-scoped tables return ZERO ROWS there — fail-closed by design. So the job
ran, read nothing, published nothing, and reported success:
`{'published': 0, 'suppressed': 0}`, which is exactly what a correctly-working
job with no trends also returns.

That is the seventh instance in this codebase of a signal that looks live and
cannot answer its own question, and it was written minutes after the paragraph
warning about this very risk. The lesson that generalises: a cross-tenant read
in this system is a SECURITY DEFINER function or it is zero rows.

Two changes came out of it. The aggregation moved into `trend_candidates()`
(migration 0015), and this job now reports `examined` alongside `published` and
`suppressed` — so "no trends" and "no data" can never again look identical.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from stylist_db.session import system_session

logger = logging.getLogger(__name__)

# Distinct tenants required before a trend is published. See the module
# docstring: this is a privacy control, not a quality threshold.
#
# 5 is the smallest number for which a single tenant's behaviour cannot
# dominate a published row, and it is the floor commonly used for released
# aggregates. It means trends DO NOT EXIST below 5 active users, which is the
# correct behaviour rather than a limitation.
MIN_COHORT_USERS = 5

# The comparison window. 14 days of "recent" against the 84 days before it,
# scaled — long enough that one weekend does not create a trend, short enough
# to be a trend rather than a season.
RECENT_DAYS = 14
BASELINE_DAYS = 84

# A value needs this many recent wears before velocity means anything. Two
# wears going to four is a 2x "trend" and is noise.
MIN_RECENT_WEARS = 10

# Values that exist in the taxonomy but can never be a trend. `unknown` is
# what a field holds when tagging could not tell; a rising `unknown` is a
# rising tagging gap, and scoring it would reward the system for garments it
# failed to identify.
_NOT_A_TREND = frozenset({"unknown"})

# A value must be rising at all before it can be a trend. Exactly 1.0 means
# "worn at its own baseline rate" — flat is not a trend.
MIN_VELOCITY = 1.0

# NO SATURATION CONSTANT, DELIBERATELY. The first version scored
# `(velocity - 1) / (SATURATION - 1)` against SATURATION = 2.0 and produced 156
# of 175 rows at exactly 1.0 — every value maximally trending, which is a
# constant, and a constant contributes nothing to a ranking. The term was dead
# again in a new way.
#
# The deeper problem was the constant itself: "which absolute velocity counts
# as high" cannot be answered without the data, so it was a magic number
# wearing a PROVISIONAL label. A trend is COMPARATIVE — "kurtas are trending"
# means relative to other subcategories — so the score is now a value's
# position among the OTHER RISING VALUES OF THE SAME FIELD. That is
# self-calibrating: it needs no threshold, it always discriminates, and the
# absolute gate above still means nothing flat or falling can appear at all.


async def compute_trends(ctx: dict[str, Any]) -> dict[str, int]:
    """Recompute `trend_signal`. Returns counts for the log.

    Replaces the table wholesale inside one transaction rather than upserting:
    a value that stopped trending must DISAPPEAR, and an upsert would leave
    yesterday's score behind forever. A stale trend is worse than none, because
    nothing about the row says it is stale.
    """
    published = 0
    suppressed_by_floor = 0
    not_rising = 0
    skipped_unknown = 0
    examined = 0

    async with system_session() as session:
        # THE AGGREGATION IS A SECURITY DEFINER FUNCTION, not a query here.
        # `system_session` has no tenant context, so reading `wear_log`
        # directly returns zero rows — see the module docstring for the bug
        # that taught this.
        result = await session.execute(
            text(
                "SELECT field_name, field_value, wears_recent, wears_baseline, users "
                "FROM trend_candidates("
                "CAST(:recent AS integer), CAST(:baseline AS integer), "
                "CAST(:min_recent AS integer))"
            ),
            {
                "recent": RECENT_DAYS,
                "baseline": BASELINE_DAYS,
                "min_recent": MIN_RECENT_WEARS,
            },
        )

        rising: list[dict[str, Any]] = []
        for row in result.mappings():
            examined += 1

            # `unknown` IS A REAL TAXONOMY VALUE AND NOT A TREND. It is what
            # `material` and `pattern` hold when tagging could not tell, so a
            # rising `unknown` is a rising TAGGING GAP — and publishing it
            # would reward the scorer for garments we failed to identify.
            if str(row["field_value"]) in _NOT_A_TREND:
                skipped_unknown += 1
                continue

            # THE FLOOR, checked before any arithmetic so a suppressed row
            # costs nothing and cannot be published by a later refactor that
            # reorders this block.
            if int(row["users"]) < MIN_COHORT_USERS:
                suppressed_by_floor += 1
                continue

            # Scale the baseline to the recent window's length so the counts
            # are comparable. Without it every value looks like it is
            # collapsing, because the baseline window holds six times the wears.
            scaled = float(row["wears_baseline"]) * (RECENT_DAYS / BASELINE_DAYS)
            # scaled == 0 means NO BASELINE: new, not trending. Treating that
            # as infinite velocity would make every first-time value the
            # strongest trend in the system, so it scores flat and falls out.
            velocity = 1.0 if scaled <= 0.0 else float(row["wears_recent"]) / scaled
            if velocity <= MIN_VELOCITY:
                # Flat or falling. Not a trend, so the table holds only things
                # that are actually rising.
                not_rising += 1
                continue

            rising.append(
                {
                    "f": str(row["field_name"]),
                    "v": str(row["field_value"])[:64],
                    "velocity": velocity,
                    "u": int(row["users"]),
                    "wr": int(row["wears_recent"]),
                    "wb": int(row["wears_baseline"]),
                }
            )

        # SCORED RELATIVE TO THE FIELD'S OTHER RISERS. See MIN_VELOCITY above
        # for why this is not an absolute threshold. Per FIELD rather than
        # globally, because velocities are not comparable across fields — there
        # are four materials for every twenty subcategories, so a global rank
        # would let the field with fewer values dominate.
        rows = _rank_within_field(rising)
        published = len(rows)

        # Wholesale replace, one transaction. See the docstring.
        await session.execute(text("DELETE FROM trend_signal"))
        for r in rows:
            await session.execute(
                text(
                    "INSERT INTO trend_signal (field_name, field_value, score, "
                    "users_contributing, wears_recent, wears_baseline) "
                    "VALUES (:f, :v, :s, :u, :wr, :wb)"
                ),
                r,
            )

    # `examined` is the one that matters when this looks wrong: 0 examined is
    # NO DATA (or a broken read), while a high `suppressed_by_floor` is the
    # k-anonymity control doing its job. Reporting only `published` made those
    # indistinguishable, which is how the RLS bug hid.
    logger.info(
        "compute_trends: examined=%d published=%d suppressed_by_floor=%d "
        "not_rising=%d skipped_unknown=%d (floor=%d users)",
        examined,
        published,
        suppressed_by_floor,
        not_rising,
        skipped_unknown,
        MIN_COHORT_USERS,
    )
    if examined == 0:
        logger.warning(
            "compute_trends examined ZERO candidates — no wear data in the last "
            "%d days, or the aggregation cannot see it",
            BASELINE_DAYS,
        )
    return {
        "examined": examined,
        "published": published,
        "suppressed_by_floor": suppressed_by_floor,
        "not_rising": not_rising,
        "skipped_unknown": skipped_unknown,
        "floor": MIN_COHORT_USERS,
    }


def _rank_within_field(rising: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score each riser by its position among the same field's other risers.

    The strongest riser in a field scores 1.0 and the weakest scores near 0.
    With a single riser the score is 1.0: it is unambiguously the field's
    trend, and there is nothing to rank it against.

    Ties share a score, so two values rising identically cannot be ordered by
    an accident of iteration — the same reason the outfit ranking tie-breaks on
    the set hash rather than on list order.
    """
    out: list[dict[str, Any]] = []
    by_field: dict[str, list[dict[str, Any]]] = {}
    for r in rising:
        by_field.setdefault(str(r["f"]), []).append(r)

    for values in by_field.values():
        velocities = sorted({float(v["velocity"]) for v in values})
        span = len(velocities) - 1
        for v in values:
            position = velocities.index(float(v["velocity"]))
            score = 1.0 if span == 0 else position / span
            out.append(
                {
                    "f": v["f"],
                    "v": v["v"],
                    "s": round(min(1.0, max(0.0, score)), 4),
                    "u": v["u"],
                    "wr": v["wr"],
                    "wb": v["wb"],
                }
            )
    return out


async def load_trends(session: Any) -> dict[tuple[str, str], float]:
    """Published trends, keyed for `trend_alignment`.

    Read through the caller's session — the table has no RLS because, by the
    floor above, a published row is not attributable to a tenant.
    """
    result = await session.execute(text("SELECT field_name, field_value, score FROM trend_signal"))
    return {(r["field_name"], r["field_value"]): float(r["score"]) for r in result.mappings()}
