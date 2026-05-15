"""OpenTelemetry tracing setup and span helpers."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Generator

import structlog
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

log = structlog.get_logger(__name__)
_tracer: trace.Tracer | None = None


def setup_tracing(service_name: str = "agentic-terraform-devops") -> None:
    global _tracer
    resource = Resource({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    exporter_type = os.environ.get("OTEL_EXPORTER", "console")
    if exporter_type == "console":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    elif exporter_type == "otlp":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter  # type: ignore[import]

        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)
    log.info("tracing.initialized", service=service_name, exporter=exporter_type)


def get_tracer() -> trace.Tracer:
    if _tracer is None:
        setup_tracing()
    return _tracer  # type: ignore[return-value]


@contextmanager
def workflow_span(name: str, attributes: dict[str, Any] | None = None) -> Generator[trace.Span, None, None]:
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as span:
        if attributes:
            for k, v in attributes.items():
                span.set_attribute(k, str(v))
        yield span


def record_workflow_event(event: str, run_id: str, **kwargs: Any) -> None:
    log.info(f"workflow.{event}", run_id=run_id, **kwargs)
    span = trace.get_current_span()
    if span.is_recording():
        span.add_event(event, {"run_id": run_id, **{k: str(v) for k, v in kwargs.items()}})
