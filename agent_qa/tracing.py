"""Bounded, HTTP-independent request tracing primitives."""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import math
import re
import secrets
import threading
import time
from typing import Iterator

from .context import get_context
from .metrics import REGISTRY
from .settings import current


_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
SPAN_NAMES = frozenset(
    {
        "http.request",
        "route_match",
        "rate_limit",
        "auth",
        "parse_body",
        "validate",
        "idempotency",
        "handler",
        "store",
        "serialize",
    }
)
_MAX_SPANS = 64
_MAX_DEPTH = 16
_SAFE_ATTRS = frozenset({"route", "operation", "store_op", "op", "errors"})
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_./{}-]{1,128}$")
_SAFE_OPERATIONS = frozenset(
    {
        "read",
        "list",
        "create",
        "update",
        "delete",
        "search",
        "count",
        "insert",
        "replace",
        "adjust",
        "claim",
        "complete",
        "cancel",
        "write",
        "get_order",
        "list_orders",
        "create_order",
        "update_order",
        "delete_order",
        "get_product",
        "list_products",
        "create_product",
        "update_product",
        "delete_product",
        "get_job",
        "list_jobs",
        "create_job",
        "update_job",
        "create_bulk",
        "adjust_stock",
        "categories",
        "export_rows",
    }
)
_SAFE_STATIC_ROUTES = frozenset(
    {
        "/ready",
        "/health",
        "/about",
        "/status",
        "/ping",
        "/metrics",
        "/fixture",
        "/version",
        "/versions",
        "/openapi.json",
        "/schemas",
        "/orders",
        "/products",
        "/jobs",
        "/whoami",
        "/webhooks",
        "/categories",
        "/admin/config",
        "/admin/keys",
        "/admin/traces",
    }
)


def parse_traceparent(value: str | None) -> tuple[str, str, str] | None:
    """Parse the supported W3C version 00 traceparent form, if valid."""
    if not isinstance(value, str) or len(value) != 55:
        return None
    match = _TRACEPARENT.fullmatch(value)
    if match is None:
        return None
    trace_id, span_id, flags = match.groups()
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    return trace_id, span_id, flags


def _safe_attrs(attrs: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in attrs.items():
        if key not in _SAFE_ATTRS:
            continue
        if key == "errors":
            if type(value) is int and 0 <= value <= 10000:
                result[key] = value
        elif isinstance(value, str) and _SAFE_VALUE.fullmatch(value):
            if key == "route":
                if value in _SAFE_STATIC_ROUTES or "{" in value:
                    result[key] = value
            elif value in _SAFE_OPERATIONS:
                result[key] = value
    return result


class TraceBuffer:
    """Thread-safe ring buffer of bounded completed trace snapshots."""

    def __init__(self, capacity: int = 100) -> None:
        self._validate_capacity(capacity)
        self._capacity = capacity
        self._items: deque[dict[str, object]] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    @staticmethod
    def _validate_capacity(capacity: int) -> None:
        if type(capacity) is not int or not 10 <= capacity <= 1000:
            raise ValueError("trace capacity must be an integer from 10 to 1000")

    @property
    def capacity(self) -> int:
        return self._capacity

    def append(self, record: dict[str, object]) -> None:
        """Append a completed record unless it is an admin trace request."""
        route = record.get("route")
        if isinstance(route, str) and route.startswith("/admin/traces"):
            return
        with self._lock:
            self._items.append(deepcopy(record))

    def list(
        self,
        *,
        route: str | None = None,
        status: int | None = None,
        min_duration_ms: float | None = None,
        limit: int = 20,
    ) -> dict[str, object]:
        """Return newest matching snapshots and bounded pagination metadata."""
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 100
        ):
            raise ValueError("limit must be an integer from 1 to 100")
        if route is not None and (
            not isinstance(route, str) or len(route) > 128 or "?" in route
        ):
            raise ValueError("invalid route filter")
        if status is not None and type(status) is not int:
            raise ValueError("invalid status filter")
        if min_duration_ms is not None and (
            isinstance(min_duration_ms, bool)
            or not isinstance(min_duration_ms, (int, float))
            or min_duration_ms < 0
            or min_duration_ms > 1_000_000_000_000
            or not math.isfinite(min_duration_ms)
        ):
            raise ValueError("invalid minimum duration")
        with self._lock:
            records = [deepcopy(item) for item in reversed(self._items)]
        matching = [
            item
            for item in records
            if (route is None or item["route"] == route)
            and (status is None or item["status"] == status)
            and (min_duration_ms is None or item["duration_ms"] >= min_duration_ms)
        ]
        return {
            "items": matching[:limit],
            "total_matching": len(matching),
            "capacity": self._capacity,
        }

    def get(self, trace_id: str) -> dict[str, object] | None:
        """Return a defensive copy of a trace by its identifier."""
        if (
            not isinstance(trace_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", trace_id) is None
        ):
            return None
        with self._lock:
            for item in reversed(self._items):
                if item.get("trace_id") == trace_id:
                    return deepcopy(item)
        return None


TRACE_BUFFER = TraceBuffer(int(current().values.get("AGENT_QA_TRACE_CAPACITY", 100)))


class Trace:
    """One request trace with bounded spans and safe attributes."""

    def __init__(self, traceparent: str | None = None) -> None:
        parsed = parse_traceparent(traceparent)
        self.trace_id = parsed[0] if parsed else secrets.token_hex(16)
        self.parent_span_id = parsed[1] if parsed else None
        self.span_id = secrets.token_hex(8)
        self._started = time.monotonic()
        self._started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._spans: list[dict[str, object]] = [
            {
                "name": "http.request",
                "start_ms": 0.0,
                "duration_ms": 0.0,
                "parent": None,
                "attrs": {},
            }
        ]
        self._stack = [0]
        self._closed = False
        self._record: dict[str, object] | None = None
        self._span_started: dict[int, float] = {0: self._started}
        self._recorded_spans: set[int] = set()

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-01"

    @contextmanager
    def span(self, name: str, **attrs: object) -> Iterator[None]:
        """Record a bounded child span, mapping unknown names to ``other``."""
        if (
            self._closed
            or len(self._spans) >= _MAX_SPANS
            or len(self._stack) >= _MAX_DEPTH
        ):
            yield
            return
        safe_name = name if name in SPAN_NAMES and name != "http.request" else "other"
        index = len(self._spans)
        started = time.monotonic()
        self._spans.append(
            {
                "name": safe_name,
                "start_ms": round((started - self._started) * 1000, 3),
                "duration_ms": 0.0,
                "parent": self._stack[-1],
                "attrs": _safe_attrs(attrs),
            }
        )
        self._span_started[index] = started
        self._stack.append(index)
        try:
            yield
        finally:
            self._close_span(index)

    def _close_span(self, index: int) -> None:
        if index in self._recorded_spans:
            return
        started = self._span_started[index]
        duration = max(0.0, time.monotonic() - started)
        self._spans[index]["duration_ms"] = round(duration * 1000, 3)
        self._recorded_spans.add(index)
        if index in self._stack:
            self._stack.remove(index)
        REGISTRY.record_span(str(self._spans[index]["name"]), duration)

    def server_timing(self) -> str:
        """Render ordered, aggregated span timing values and total duration."""
        aggregate: dict[str, float] = {}
        for item in self._spans:
            name = str(item["name"])
            aggregate[name] = aggregate.get(name, 0.0) + float(item["duration_ms"])
        total_ms = max(0.0, (time.monotonic() - self._started) * 1000)
        if self._record is not None:
            total_ms = float(self._record["duration_ms"])
        values = [f"{name};dur={duration:.3f}" for name, duration in aggregate.items()]
        values.append(f"total;dur={total_ms:.3f}")
        return ", ".join(values)

    def finish(
        self,
        *,
        request_id: str,
        method: str,
        route: str,
        status: int,
        record: bool = True,
    ) -> dict[str, object]:
        """Close the root span and return its safe record snapshot."""
        if self._record is not None:
            return deepcopy(self._record)
        for index in tuple(self._stack[1:]):
            self._close_span(index)
        duration = max(0.0, time.monotonic() - self._started)
        self._spans[0]["duration_ms"] = round(duration * 1000, 3)
        if 0 not in self._recorded_spans:
            self._recorded_spans.add(0)
            REGISTRY.record_span("http.request", duration)
        safe_route = (
            route if isinstance(route, str) and len(route) <= 128 else "unmatched"
        )
        self._record = {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "request_id": str(request_id)[:128],
            "method": str(method)[:16],
            "route": safe_route,
            "status": status if type(status) is int and 100 <= status <= 599 else 500,
            "duration_ms": round(duration * 1000, 3),
            "started_at": self._started_at,
            "spans": deepcopy(self._spans),
        }
        self._closed = True
        if record:
            TRACE_BUFFER.append(self._record)
        return deepcopy(self._record)


@contextmanager
def span(name: str, **attrs: object) -> Iterator[None]:
    """Record a span on the thread-local request context when one is active."""
    context = get_context()
    trace = context.trace if context is not None else None
    if isinstance(trace, Trace):
        with trace.span(name, **attrs):
            yield
    else:
        yield
