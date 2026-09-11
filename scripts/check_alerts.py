"""Evaluate the four alerts and exit non-zero if any is firing.

For a cron, a container healthcheck, or CI. The HTTP endpoint is for humans and
dashboards; this is the machine-readable edge:

    0  nothing firing
    1  at least one alert firing   (the message names which, and what to do)
    2  could not evaluate          (the API is unreachable)

Exit 2 is deliberately distinct: "I could not tell" must never be reported as
"everything is fine", which is the same mistake the ops aggregates made when
RLS silently returned zero rows.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid

import httpx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--json", action="store_true", help="emit the raw payload")
    args = ap.parse_args()

    try:
        with httpx.Client(base_url=args.base, timeout=30) as client:
            # The ops surface requires a token. Phase 5 has no admin plane, so
            # this registers a throwaway account rather than pretending there
            # is a service credential — see the ops router's KNOWN GAP note.
            reg = client.post(
                "/auth/register",
                json={
                    "email": f"alertcheck-{uuid.uuid4().hex[:10]}@example.com",
                    "password": "a-long-enough-password",
                },
            )
            reg.raise_for_status()
            token = reg.json()["access_token"]
            resp = client.get("/ops/alerts", headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:
        print(f"could not evaluate alerts: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(body, indent=2))
        return 1 if body["firing"] else 0

    for alert in body["alerts"]:
        mark = "FIRING " if alert["firing"] else "ok     "
        print(f"  {mark} {alert['alert']}")
        if alert["firing"]:
            print(f"          -> {alert['response']}")
    print(f"\n{body['firing']} of {len(body['alerts'])} firing")
    return 1 if body["firing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
