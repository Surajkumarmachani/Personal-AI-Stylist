"""Alerting hook.

Phase 5 wires real observability (OTel + Langfuse + dashboards). Until then
alerts are structured ERROR logs with a stable `event` field, which is enough
to (a) be scraped by any log-based alerting rule and (b) be asserted in tests —
the Phase 2 exit criterion is that a poison image reaches the DLQ *and an alert
fires*, so the alert has to be an observable thing, not a TODO.

The four Phase 5 alerts this feeds: DLQ age > 1h, daily spend > 2x trailing
mean, ingest success rate < 99%, API 5xx rate. Resist adding more — unactionable
alerts train people to ignore pages.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("stylist.alerts")

# Every alert name that exists. A typo'd name is an alert that never fires and
# nobody notices, so the set is closed and checked.
KNOWN_ALERTS: frozenset[str] = frozenset(
    {
        "dlq.job_parked",
        "relay.backlog_growing",
        "ingest.success_rate_low",
    }
)


def alert(name: str, **fields: Any) -> None:
    if name not in KNOWN_ALERTS:
        raise ValueError(f"unknown alert {name!r}; add it to KNOWN_ALERTS")
    # NEVER log image bytes, presigned URLs or raw model output (§D3). Callers
    # pass ids and short reasons; this is the reminder that it matters.
    logger.error("ALERT %s %s", name, json.dumps(fields, sort_keys=True), extra={"alert": name})
