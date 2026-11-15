"""Optional OpenTelemetry bridge.

``agent_events`` is the system of record. When OTEL_EXPORTER_OTLP_ENDPOINT is
set and the ``otel`` extra is installed, spans are mirrored to a collector as
well so reviews show up next to the rest of your services.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def setup_tracer(endpoint: str, service_name: str = "pr-review-agent") -> Any | None:
    if not endpoint:
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("OTEL endpoint set but opentelemetry is not installed; run: pip install '.[otel]'")
        return None
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)
