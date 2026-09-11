"""OpenTelemetry tracing, with a working no-op when it is not configured.

OFF BY DEFAULT, AND OFF MEANS ZERO COST
---------------------------------------
Tracing activates only when OTEL_EXPORTER_OTLP_ENDPOINT is set. With no
endpoint there is no collector to receive spans, and a tracer that buffers them
for a destination that does not exist is a memory leak with extra steps. The
no-op path does not import the SDK at all.

WHY A SPAN PER STAGE AND NOT PER JOB
------------------------------------
"An ingest took 25 seconds" is where an investigation stops. That exact
question cost a long detour earlier in this project: the ml endpoints summed to
~6s and there was no way to attribute the other 19 — which turned out to be
queue wait, not processing, a distinction a per-job span cannot express and a
per-stage span makes obvious at a glance.

The per-stage log lines in the state machine were the field expedient. This is
the same data in a form that survives aggregation across replicas.

IMPORT FAILURE IS NOT AN OUTAGE
-------------------------------
If the SDK is absent but the endpoint is set — an incomplete install — this
logs once and degrades to the no-op. An ingest pipeline that refuses to run
because it cannot report on itself has its priorities backwards.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

_tracer: Any | None = None
_configured = False


def _endpoint() -> str | None:
    return os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None


def configure_tracing(service_name: str) -> bool:
    """Set up the tracer. Returns whether tracing is actually on.

    Idempotent: arq's worker startup and uvicorn's reloader both call this more
    than once, and installing a second exporter would double every span.
    """
    global _tracer, _configured
    if _configured:
        return _tracer is not None
    _configured = True

    endpoint = _endpoint()
    if not endpoint:
        logger.info("tracing disabled (OTEL_EXPORTER_OTLP_ENDPOINT unset)")
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        # Endpoint set, SDK missing. Say so loudly — silence here looks exactly
        # like "tracing is on and nothing is happening".
        logger.warning(
            "OTEL_EXPORTER_OTLP_ENDPOINT is set but the SDK is not installed (%s); "
            "install the 'observability' extra. Continuing untraced.",
            exc,
        )
        return False

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)
    logger.info("tracing enabled -> %s", endpoint)
    return True


@contextmanager
def stage_span(name: str, **attributes: Any) -> Iterator[None]:
    """One span per pipeline stage. A no-op when tracing is off."""
    if _tracer is None:
        yield
        return
    with _tracer.start_as_current_span(name) as span:
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, str(value))
        try:
            yield
        except Exception as exc:
            # Record the failure ON the span. A stage that failed and a stage
            # that was never reached look identical otherwise, and the whole
            # point of the trace is telling those apart.
            span.record_exception(exc)
            from opentelemetry.trace import Status, StatusCode

            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


def traced() -> bool:
    return _tracer is not None
