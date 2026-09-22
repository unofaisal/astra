"""Observability: optional OpenTelemetry spans + a tiny in-process
metrics registry.

* Spans follow the OpenTelemetry GenAI semantic conventions
  (``execute_tool`` / ``chat`` operation names, ``gen_ai.tool.*`` and
  ``gen_ai.conversation.id`` attributes).  If ``opentelemetry-api`` is not
  installed — or no SDK/exporter is configured — every call is a cheap
  no-op, so the core stays dependency-free.
* ``metrics`` is a plain in-process registry (counters, gauges, histograms
  with fixed buckets).  Read it with ``metrics.snapshot()`` (the example
  API exposes it at ``/metrics``) or export it yourself.

The gen_ai.* namespace is still marked experimental upstream; the core
attribute names used here have been stable in shape.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Any, Dict, Iterator, Optional

try:  # optional dependency
    from opentelemetry import trace as _otel_trace  # type: ignore
    from opentelemetry.trace import Status, StatusCode  # type: ignore

    _tracer = _otel_trace.get_tracer("astra")
except Exception:  # pragma: no cover - exercised only without otel installed
    _otel_trace = None
    _tracer = None
    Status = StatusCode = None  # type: ignore


class _NullSpan:
    def set_attribute(self, *a: Any, **k: Any) -> None: ...
    def record_exception(self, *a: Any, **k: Any) -> None: ...
    def set_status(self, *a: Any, **k: Any) -> None: ...


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Start a span (or a null span when OpenTelemetry isn't available)."""
    if _tracer is None:
        yield _NullSpan()
        return
    with _tracer.start_as_current_span(name) as sp:
        for k, v in attributes.items():
            if v is not None:
                try:
                    sp.set_attribute(k, v)
                except Exception:
                    pass
        try:
            yield sp
        except BaseException as exc:
            try:
                sp.record_exception(exc)
                sp.set_status(Status(StatusCode.ERROR, str(exc)))
            except Exception:
                pass
            raise


def otel_enabled() -> bool:
    return _tracer is not None


# ── metrics ────────────────────────────────────────────────────────

_DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)


class _Histogram:
    def __init__(self, buckets=_DEFAULT_BUCKETS) -> None:
        self.buckets = tuple(buckets)
        self.counts = [0] * (len(self.buckets) + 1)
        self.sum = 0.0
        self.count = 0

    def observe(self, value: float) -> None:
        self.sum += value
        self.count += 1
        for i, b in enumerate(self.buckets):
            if value <= b:
                self.counts[i] += 1
                return
        self.counts[-1] += 1

    def as_dict(self) -> dict:
        return {"count": self.count, "sum": round(self.sum, 6), "buckets": dict(zip([*map(str, self.buckets), "+Inf"], self.counts))}


def _key(name: str, labels: Optional[Dict[str, Any]]) -> str:
    if not labels:
        return name
    inner = ",".join(f"{k}={labels[k]}" for k in sorted(labels))
    return f"{name}{{{inner}}}"


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, float] = {}
        self._gauges: Dict[str, float] = {}
        self._hists: Dict[str, _Histogram] = {}

    def inc(self, name: str, value: float = 1, **labels: Any) -> None:
        k = _key(name, labels)
        with self._lock:
            self._counters[k] = self._counters.get(k, 0) + value

    def gauge_add(self, name: str, delta: float, **labels: Any) -> None:
        k = _key(name, labels)
        with self._lock:
            self._gauges[k] = self._gauges.get(k, 0) + delta

    def gauge_set(self, name: str, value: float, **labels: Any) -> None:
        with self._lock:
            self._gauges[_key(name, labels)] = value

    def observe(self, name: str, value: float, **labels: Any) -> None:
        k = _key(name, labels)
        with self._lock:
            h = self._hists.get(k)
            if h is None:
                h = self._hists[k] = _Histogram()
            h.observe(value)

    @contextlib.contextmanager
    def timer(self, name: str, **labels: Any) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, time.perf_counter() - t0, **labels)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "histograms": {k: h.as_dict() for k, h in self._hists.items()},
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._hists.clear()


metrics = Metrics()
