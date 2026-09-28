"""Logging setup shared by api, worker and ml.

Plain text locally, one JSON object per line when LOG_FORMAT=json (every
deployed service; see infra/gcp/deploy.sh).

WHY JSON IN PRODUCTION
----------------------
Cloud Logging reads `severity` only from a structured line. A plain-text line
written to stderr (where Python logging writes by default) arrives as a single
opaque string, so ERROR and INFO look the same. A log-based alert on "severity
>= ERROR" then either never fires or fires on everything. `message` carrying the
traceback is also what lets Error Reporting group exceptions.

Uvicorn's own loggers are rewired too: they install their own handlers with
propagate=False, so configuring the root logger alone would leave server
errors as the one unstructured stream in production.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        entry = {
            "severity": record.levelname,
            "message": message,
            "logger": record.name,
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
        }
        return json.dumps(entry, default=str)


def configure_logging(level: str | int = "INFO") -> None:
    if os.environ.get("LOG_FORMAT", "text").lower() != "json":
        logging.basicConfig(level=level)
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = [handler]
        lg.propagate = False
