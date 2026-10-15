"""Tracing and metrics with redaction BEFORE export.

Traces are not the audit log (that is `secret_access_log`, `authz_log`, `task_events`, `skill_events`, complete and
append-only in SQLite). Traces are for debugging and cost, so they carry identifiers, verdicts, counts and
lengths, never page text, screenshots, prompts, completions, cookies or secret-shaped strings.

Every span passes through `RedactingExporter`, which rebuilds it with sanitised attributes, so even a span created by a
library we do not control cannot export a value we would have blocked.
"""

import hashlib
import logging
import os
from collections import Counter
from collections.abc import Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode

from app.security.secrets import redact

log = logging.getLogger("nami.telemetry")

BLOCKED_KEYS = ("page_text", "screenshot", "dom", "html", "prompt", "completion", "secret", "password", "cookie",
                "authorization", "token", "api_key", "sensitive", "card", "cvv", "otp", "seed", "body", "content")
MAX_VALUE = 200


def sanitize_attrs(attrs: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (attrs or {}).items():
        lk = k.lower()
        if any(b in lk for b in BLOCKED_KEYS) and not lk.endswith(("_len", "_count", "_sha", "_hash", ".verdict", "_chars")):
            continue                                               # dropped entirely, not masked
        if isinstance(v, (bytes, bytearray)):
            continue
        if isinstance(v, str):
            v = redact(v)[:MAX_VALUE]
        elif isinstance(v, (list, tuple)):
            v = [redact(x)[:MAX_VALUE] if isinstance(x, str) else x for x in v if not isinstance(x, (bytes, bytearray))]
        out[k] = v
    return out


class RedactingExporter(SpanExporter):
    def __init__(self, inner: SpanExporter):
        self.inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        clean = []
        for s in spans:
            events = tuple(type(e)(e.name, sanitize_attrs(dict(e.attributes or {})), e.timestamp) for e in s.events)
            status = s.status
            if status and status.description:
                status = Status(status.status_code, redact(status.description)[:MAX_VALUE])
            clean.append(ReadableSpan(name=s.name, context=s.context, parent=s.parent, resource=s.resource,
                                      attributes=sanitize_attrs(dict(s.attributes or {})), events=events, links=s.links,
                                      kind=s.kind, status=status, start_time=s.start_time, end_time=s.end_time,
                                      instrumentation_scope=s.instrumentation_scope))
        return self.inner.export(clean)

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


current_task: ContextVar[str | None] = ContextVar("nami_current_task", default=None)


class Telemetry:
    """Disabled until `configure()`; every call is then a cheap no-op, so instrumented code never needs a guard."""

    def __init__(self) -> None:
        self.enabled = False
        self.counters: Counter = Counter()
        self._tracer = None
        self._provider: TracerProvider | None = None
        self._salt = ""
        self.usage: dict[str, dict[str, int]] = {}          # per task: tokens and calls, kept even when tracing is off

    def add_usage(self, input_tokens: int, output_tokens: int) -> None:
        tid = current_task.get()
        if tid:
            u = self.usage.setdefault(tid, {"input_tokens": 0, "output_tokens": 0, "calls": 0})
            u["input_tokens"] += input_tokens
            u["output_tokens"] += output_tokens
            u["calls"] += 1

    def pop_usage(self, task_id: str) -> dict[str, int]:
        return self.usage.pop(task_id, {"input_tokens": 0, "output_tokens": 0, "calls": 0})

    def configure(self, exporter: SpanExporter | None = None, *, otlp_endpoint: str | None = None,
                  headers: dict[str, str] | None = None, salt: str = "", service: str = "nami") -> None:
        if exporter is None and otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            exporter = OTLPSpanExporter(endpoint=otlp_endpoint, headers=headers or {})
        self._salt = salt or os.urandom(8).hex()
        self._provider = TracerProvider(resource=Resource.create({"service.name": service}))
        if exporter is not None:
            self._provider.add_span_processor(SimpleSpanProcessor(RedactingExporter(exporter)))
        self._tracer = self._provider.get_tracer("nami")
        self.enabled = True

    def shutdown(self) -> None:
        if self._provider:
            self._provider.shutdown()
        self.enabled, self._provider, self._tracer = False, None, None

    def user_hash(self, user_id: str) -> str:
        return hashlib.sha256((self._salt + user_id).encode()).hexdigest()[:12]

    def inc(self, name: str, **labels: str) -> None:
        if self.enabled:
            self.counters[(name, tuple(sorted(labels.items())))] += 1

    def count(self, name: str, **labels: str) -> int:
        return self.counters[(name, tuple(sorted(labels.items())))]

    @contextmanager
    def span(self, name: str, *, org_id: str | None = None, user_id: str | None = None, task_id: str | None = None,
             **attrs: Any):
        if not self.enabled or self._tracer is None:
            yield None
            return
        base = {"nami.org_id": org_id, "nami.user_hash": self.user_hash(user_id) if user_id else None, "nami.task_id": task_id}
        with self._tracer.start_as_current_span(name) as sp:
            for k, v in {**{k: v for k, v in base.items() if v}, **sanitize_attrs(attrs)}.items():
                sp.set_attribute(k, v)
            try:
                yield sp
            except Exception as e:
                sp.set_status(Status(StatusCode.ERROR, type(e).__name__))
                sp.record_exception(e)
                raise


tel = Telemetry()
