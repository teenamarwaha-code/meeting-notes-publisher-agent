"""OpenTelemetry distributed tracing, Structured JSON Logging, and Intent-vs-Outcome capture.

Implements all four Observability & Tracing pillars:
1. Structured JSON Logging compatible with Google Cloud Logging (`severity`, `logging.googleapis.com/trace`).
2. Explicit `INTENT` (pre-execution) vs. `OUTCOME` (post-execution) event capture on every tool/agent action.
3. OpenTelemetry distributed span hierarchy linking root coordinator -> subagent -> tool spans.
4. Automatic PII redaction on all log payloads and span attributes before emission.
"""

from __future__ import annotations

from contextlib import contextmanager
import datetime
import functools
import json
import logging
import os
import time
from typing import Any, Callable, Iterator, TypeVar
import uuid

from .pii_redactor import PIIRedactor, get_pii_redactor

try:
    from opentelemetry import trace as otel_trace  # type: ignore
    from opentelemetry.sdk.resources import Resource  # type: ignore
    from opentelemetry.sdk.trace import TracerProvider  # type: ignore
    from opentelemetry.sdk.trace.export import (  # type: ignore
        BatchSpanProcessor,
        SimpleSpanProcessor,
        SpanExporter,
        SpanExportResult,
    )

    HAS_OTEL = True
except ImportError:  # pragma: no cover - clean fallback when running in zero-dep environments
    otel_trace = None
    HAS_OTEL = False

F = TypeVar("F", bound=Callable[..., Any])


class InMemorySpanRecord:
    """Portable span record persisted for live UI inspection and automated test assertions."""

    def __init__(
        self,
        name: str,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.start_time = time.time()
        self.end_time: float | None = None
        self.status: str = "OK"
        self.attributes: dict[str, Any] = dict(attributes or {})

    @property
    def duration_ms(self) -> float:
        end = self.end_time if self.end_time is not None else time.time()
        return round((end - self.start_time) * 1000.0, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "attributes": self.attributes,
        }


class StructuredJsonFormatter(logging.Formatter):
    """Formats Python `LogRecord` entries as single-line Cloud Logging JSON objects."""

    def __init__(self, redactor: PIIRedactor | None = None) -> None:
        super().__init__()
        self.redactor = redactor or get_pii_redactor()

    def format(self, record: logging.LogRecord) -> str:
        base_payload: dict[str, Any] = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": self.redactor.scrub_text(record.getMessage()),
        }
        structured_extra = getattr(record, "structured_data", None)
        if isinstance(structured_extra, dict):
            base_payload.update(self.redactor.scrub_payload(structured_extra))
        return json.dumps(base_payload, default=str, sort_keys=True)


class StructuredJsonLogger:
    """Structured JSON logger with built-in Intent vs. Outcome capture and ring buffer."""

    def __init__(
        self,
        name: str = "session_publisher_agent",
        redactor: PIIRedactor | None = None,
    ) -> None:
        self.redactor = redactor or get_pii_redactor()
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(StructuredJsonFormatter(redactor=self.redactor))
            self.logger.addHandler(handler)
        self.events: list[dict[str, Any]] = []

    def emit(self, severity: str, message: str, **metadata: Any) -> dict[str, Any]:
        """Emits a PII-scrubbed structured JSON log entry."""
        scrubbed_meta = self.redactor.scrub_payload(metadata)
        scrubbed_msg = self.redactor.scrub_text(message)
        entry = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "severity": severity.upper(),
            "message": scrubbed_msg,
            **scrubbed_meta,
        }
        self.events.append(entry)
        if len(self.events) > 500:
            self.events = self.events[-500:]

        level = getattr(logging, severity.upper(), logging.INFO)
        self.logger.log(level, scrubbed_msg, extra={"structured_data": scrubbed_meta})
        return entry

    def log_intent(
        self,
        component: str,
        intended_action: str,
        trace_id: str,
        span_id: str,
        inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Records the agent's explicit INTENT prior to executing a tool or subagent."""
        return self.emit(
            "INFO",
            f"INTENT: {component} -> {intended_action}",
            event_phase="INTENT",
            component=component,
            intended_action=intended_action,
            trace_id=trace_id,
            span_id=span_id,
            inputs=inputs or {},
        )

    def log_outcome(
        self,
        component: str,
        intended_action: str,
        actual_outcome: str,
        status: str,
        latency_ms: float,
        trace_id: str,
        span_id: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Records the actual OUTCOME and latency after executing a tool or subagent."""
        severity = "INFO" if status == "SUCCESS" else ("WARNING" if status == "BLOCKED_HITL" else "ERROR")
        return self.emit(
            severity,
            f"OUTCOME: {component} -> {actual_outcome} ({status})",
            event_phase="OUTCOME",
            component=component,
            intended_action=intended_action,
            actual_outcome=actual_outcome,
            status=status,
            latency_ms=round(latency_ms, 2),
            trace_id=trace_id,
            span_id=span_id,
            details=details or {},
        )


class TelemetryTracer:
    """OpenTelemetry-backed distributed tracer with Cloud Trace export and span buffer."""

    def __init__(
        self,
        service_name: str = "session-publisher-agent",
        redactor: PIIRedactor | None = None,
    ) -> None:
        self.service_name = service_name
        self.redactor = redactor or get_pii_redactor()
        self.recorded_spans: list[InMemorySpanRecord] = []
        self._active_stack: list[InMemorySpanRecord] = []
        self._otel_tracer = None

        if HAS_OTEL and otel_trace is not None:
            provider = TracerProvider(
                resource=Resource.create({"service.name": service_name})
            )
            if os.getenv("ENABLE_CLOUD_TRACE_EXPORT", "false").lower() == "true":  # pragma: no cover
                try:
                    from opentelemetry.exporter.cloud_trace import (  # type: ignore
                        CloudTraceSpanExporter,
                    )

                    provider.add_span_processor(
                        BatchSpanProcessor(CloudTraceSpanExporter())
                    )
                except Exception:
                    pass
            self._otel_tracer = provider.get_tracer(service_name)

    @property
    def current_trace_id(self) -> str:
        if self._active_stack:
            return self._active_stack[-1].trace_id
        return uuid.uuid4().hex

    @property
    def current_span_id(self) -> str | None:
        if self._active_stack:
            return self._active_stack[-1].span_id
        return None

    @contextmanager
    def start_span(
        self, name: str, attributes: dict[str, Any] | None = None
    ) -> Iterator[InMemorySpanRecord]:
        """Starts a child span linked to any active parent trace/span."""
        scrubbed_attrs = self.redactor.scrub_payload(attributes or {})
        parent = self._active_stack[-1] if self._active_stack else None
        trace_id = parent.trace_id if parent else uuid.uuid4().hex
        span_id = uuid.uuid4().hex[:16]
        record = InMemorySpanRecord(
            name=name,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent.span_id if parent else None,
            attributes=scrubbed_attrs,
        )
        self._active_stack.append(record)

        if self._otel_tracer is not None:
            with self._otel_tracer.start_as_current_span(name) as otel_span:
                for k, v in scrubbed_attrs.items():
                    otel_span.set_attribute(str(k), str(v))
                try:
                    yield record
                except Exception as exc:
                    record.status = f"ERROR: {type(exc).__name__}"
                    raise
                finally:
                    record.end_time = time.time()
                    self._active_stack.pop()
                    self.recorded_spans.append(record)
        else:
            try:
                yield record
            except Exception as exc:
                record.status = f"ERROR: {type(exc).__name__}"
                raise
            finally:
                record.end_time = time.time()
                self._active_stack.pop()
                self.recorded_spans.append(record)


_DEFAULT_LOGGER: StructuredJsonLogger | None = None
_DEFAULT_TRACER: TelemetryTracer | None = None


def get_structured_logger() -> StructuredJsonLogger:
    global _DEFAULT_LOGGER
    if _DEFAULT_LOGGER is None:
        _DEFAULT_LOGGER = StructuredJsonLogger()
    return _DEFAULT_LOGGER


def get_tracer() -> TelemetryTracer:
    global _DEFAULT_TRACER
    if _DEFAULT_TRACER is None:
        _DEFAULT_TRACER = TelemetryTracer()
    return _DEFAULT_TRACER


def trace_intent_and_outcome(component: str, action_description: str) -> Callable[[F], F]:
    """Decorator wrapping tool/agent functions in an OTel span + INTENT/OUTCOME logs."""

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = get_tracer()
            slogger = get_structured_logger()
            start = time.perf_counter()
            with tracer.start_span(
                name=f"{component}.{func.__name__}",
                attributes={"component": component, "action": action_description},
            ) as span:
                slogger.log_intent(
                    component=component,
                    intended_action=action_description,
                    trace_id=span.trace_id,
                    span_id=span.span_id,
                    inputs={"kwargs": kwargs},
                )
                try:
                    result = func(*args, **kwargs)
                    elapsed_ms = (time.perf_counter() - start) * 1000.0
                    status = "SUCCESS"
                    outcome_summary = "Completed successfully"
                    if isinstance(result, dict):
                        if result.get("status") == "error":
                            status = "GUIDED_ERROR"
                            outcome_summary = str(result.get("error_code", "Tool validation/execution error"))
                        elif result.get("status") == "pending_hitl_approval":
                            status = "BLOCKED_HITL"
                            outcome_summary = "Paused at Human-in-the-Loop approval gate"
                    span.attributes["outcome_status"] = status
                    slogger.log_outcome(
                        component=component,
                        intended_action=action_description,
                        actual_outcome=outcome_summary,
                        status=status,
                        latency_ms=elapsed_ms,
                        trace_id=span.trace_id,
                        span_id=span.span_id,
                    )
                    return result
                except Exception as exc:
                    elapsed_ms = (time.perf_counter() - start) * 1000.0
                    slogger.log_outcome(
                        component=component,
                        intended_action=action_description,
                        actual_outcome=f"Unhandled {type(exc).__name__}: {exc}",
                        status="EXCEPTION",
                        latency_ms=elapsed_ms,
                        trace_id=span.trace_id,
                        span_id=span.span_id,
                    )
                    raise

        return wrapper  # type: ignore[return-value]

    return decorator
