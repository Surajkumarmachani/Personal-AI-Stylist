"""Operational surface: the four alerts and the queries behind the dashboards.

EXACTLY FOUR ALERTS
-------------------
The plan is emphatic: "Resist adding more; unactionable alerts train people to
ignore pages." Each of these four has a defined response, which is the test for
whether an alert deserves to exist:

  dlq_age          -> something is stuck. Look at the DLQ'd job's last_error.
  spend_spike      -> a loop, a retry storm, or a pricing change. Check
                      model_calls grouped by model.
  ingest_success    -> the pipeline is dropping work. Look at stage_attempts.
  api_5xx          -> the API itself is failing. Look at the api logs.

An alert with no defined response is a notification, and notifications belong
on a dashboard.

WHY THESE ARE SYSTEM-WIDE, AND HOW THEY READ ACROSS TENANTS
-----------------------------------------------------------
Unlike /ops/correction-rate, which is deliberately tenant-scoped, these read
across all tenants — an operator needs to know the DLQ is backing up regardless
of whose job it is.

They do NOT do that with a session that bypasses RLS. The API runs as
`stylist_app` (NOSUPERUSER, NOBYPASSRLS) and every tenant table is FORCE RLS,
so a plain cross-tenant query here returns ZERO ROWS — which is not an error.
`count(*) = 0` reads as "the DLQ is empty" and a rate over no samples reads as
"nothing is failing", so the alerts would be permanently, invisibly green.
That was the first version of this file and it is the reason migration 0006
exists.

Instead every aggregate goes through a SECURITY DEFINER function (0006). The
function runs with the owner's rights; the API holds none. The whole privileged
surface is seven functions that return counts, rates and timestamps — there is
no argument to any of them that can yield a garment, an image key or an email.

In production this router still belongs behind an admin authorisation boundary
rather than a user token. It is mounted here because Phase 5 has no admin
plane yet, and the alternative — no operational visibility at all — is worse.
This is a KNOWN GAP, not an oversight.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import text

from stylist_api.deps import CacheRedisDep, CurrentUser
from stylist_db.session import system_session

router = APIRouter(tags=["ops"])

# ---------------------------------------------------------------- thresholds
# Named constants rather than literals in the queries, because these are the
# numbers that get argued about and they should be greppable.
DLQ_AGE_ALERT_SECONDS = 3600.0  # "DLQ age > 1h"
SPEND_SPIKE_MULTIPLE = 2.0  # "daily spend > 2x trailing mean"
SPEND_TRAILING_DAYS = 7
INGEST_SUCCESS_FLOOR = 0.99  # "ingest success rate < 99%"
API_5XX_CEILING = 0.01  # 1% of requests
# Below this, a rate is noise: 2 failures out of 3 requests is 67% and means
# nothing. Alerting on it teaches people the alert is wrong.
MIN_SAMPLE_FOR_RATE = 20


async def _dlq_age(session: Any) -> dict[str, Any]:
    row = await session.execute(text("SELECT depth, oldest_seconds FROM ops_dlq_stats()"))
    r = row.mappings().one()
    oldest = float(r["oldest_seconds"])
    return {
        "alert": "dlq_age",
        "firing": oldest > DLQ_AGE_ALERT_SECONDS,
        "value_seconds": round(oldest, 1),
        "threshold_seconds": DLQ_AGE_ALERT_SECONDS,
        "dlq_depth": int(r["depth"]),
        "response": "inspect jobs.last_error for the oldest DLQ'd job",
    }


async def _spend_spike(session: Any) -> dict[str, Any]:
    row = await session.execute(
        text("SELECT today, trailing_mean, trailing_days_seen FROM ops_spend_stats(:days)"),
        {"days": SPEND_TRAILING_DAYS},
    )
    r = row.mappings().one()
    today = float(r["today"] or 0)
    mean = float(r["trailing_mean"] or 0)
    # With no history there is no baseline, so nothing can be 2x it. Firing
    # here would mean every new deployment pages someone on day one.
    firing = bool(
        int(r["trailing_days_seen"]) >= 2 and mean > 0 and today > SPEND_SPIKE_MULTIPLE * mean
    )
    return {
        "alert": "spend_spike",
        "firing": firing,
        "today_usd": round(today, 6),
        "trailing_mean_usd": round(mean, 6),
        "multiple": round(today / mean, 2) if mean else None,
        "threshold_multiple": SPEND_SPIKE_MULTIPLE,
        "response": "group model_calls by model over the last day",
    }


async def _ingest_success(session: Any, window_hours: int) -> dict[str, Any]:
    row = await session.execute(
        text("SELECT ok, dead, total FROM ops_ingest_stats(:h)"), {"h": window_hours}
    )
    r = row.mappings().one()
    ok, dead, total = int(r["ok"]), int(r["dead"]), int(r["total"])
    settled = ok + dead
    rate = (ok / settled) if settled else None
    return {
        "alert": "ingest_success",
        # Only SETTLED jobs count. Including in-flight jobs makes the rate dip
        # every time a burst arrives, which is the one moment you least want a
        # spurious page.
        "firing": bool(
            settled >= MIN_SAMPLE_FOR_RATE and rate is not None and rate < INGEST_SUCCESS_FLOOR
        ),
        "rate": round(rate, 4) if rate is not None else None,
        "threshold": INGEST_SUCCESS_FLOOR,
        "completed": ok,
        "dlq": dead,
        "in_flight": total - settled,
        "window_hours": window_hours,
        "response": "inspect jobs.stage_attempts for the failing stage",
    }


async def _api_5xx(cache: Any, window_minutes: int) -> dict[str, Any]:
    """Read the counters the request middleware writes.

    In Redis rather than in-process because the rate has to be a property of
    the SERVICE, not of whichever replica happens to answer /ops/alerts.
    """
    from stylist_api.middleware import status_counts

    counts = await status_counts(cache, window_minutes)
    total = sum(counts.values())
    server_errors = counts.get("5xx", 0)
    rate = (server_errors / total) if total else None
    return {
        "alert": "api_5xx",
        "firing": bool(
            total >= MIN_SAMPLE_FOR_RATE and rate is not None and rate > API_5XX_CEILING
        ),
        "rate": round(rate, 4) if rate is not None else None,
        "threshold": API_5XX_CEILING,
        "counts": counts,
        "window_minutes": window_minutes,
        "response": "check the api container logs for tracebacks",
    }


@router.get("/ops/alerts")
async def alerts(
    user: CurrentUser,
    cache: CacheRedisDep,
    window_hours: Annotated[int, Query(ge=1, le=168)] = 24,
    window_minutes: Annotated[int, Query(ge=1, le=1440)] = 15,
) -> dict[str, Any]:
    """All four alerts, each with its firing state and the response to take."""
    async with system_session() as session:
        checks = [
            await _dlq_age(session),
            await _spend_spike(session),
            await _ingest_success(session, window_hours),
            await _api_5xx(cache, window_minutes),
        ]
    firing = [c for c in checks if c["firing"]]
    return {
        "firing": len(firing),
        "alerts": checks,
        # A flat count so a probe can alert on one number without parsing.
        "ok": len(firing) == 0,
    }


@router.get("/ops/dashboards")
async def dashboards(user: CurrentUser) -> dict[str, Any]:
    """The four dashboards, as data rather than as a Grafana JSON blob.

    There is no Grafana in this stack, and shipping a dashboard definition for
    a tool nobody is running would be decoration. These are the four questions
    §D3 says an operator asks, answered directly — so the numbers exist and are
    correct before there is somewhere pretty to draw them.
    """
    async with system_session() as session:
        funnel = await session.execute(text("SELECT state, n FROM ops_funnel(24)"))
        latency = await session.execute(text("SELECT p50, p95, n FROM ops_latency(24)"))
        spend = await session.execute(
            text("SELECT model_name, calls, usd, avg_ms, cached FROM ops_model_spend(24)")
        )
        corrections = await session.execute(
            text("SELECT field_name, n, avg_conf FROM ops_correction_rate(30)")
        )
        lat = latency.mappings().one()
        return {
            "ingest_funnel_24h": [dict(r) for r in funnel.mappings()],
            "ingest_latency_24h": {
                "p50_seconds": float(lat["p50"]) if lat["p50"] is not None else None,
                "p95_seconds": float(lat["p95"]) if lat["p95"] is not None else None,
                "samples": int(lat["n"]),
                # The plan's Phase 5 criterion.
                "p95_within_60s": (float(lat["p95"]) < 60.0) if lat["p95"] is not None else None,
            },
            "model_spend_24h": [
                {
                    "model": r["model_name"],
                    "calls": int(r["calls"]),
                    "usd": float(r["usd"] or 0),
                    "avg_ms": int(r["avg_ms"] or 0),
                    "cache_hits": int(r["cached"]),
                }
                for r in spend.mappings()
            ],
            "correction_rate_30d": [
                {
                    "field": r["field_name"],
                    "corrections": int(r["n"]),
                    "avg_model_confidence": r["avg_conf"],
                }
                for r in corrections.mappings()
            ],
        }
