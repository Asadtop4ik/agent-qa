"""Thread-safe, HTTP-independent Prometheus metrics registry."""

from __future__ import annotations

import threading


_REQUESTS_NAME = "agent_qa_http_requests_total"
_DURATION_NAME = "agent_qa_http_request_duration_seconds"
_ORDERS_NAME = "agent_qa_orders"
_PRODUCTS_NAME = "agent_qa_products"
_BUILD_NAME = "agent_qa_build_info"
_IDEMPOTENCY_NAME = "agent_qa_idempotency_total"
_RATE_LIMITED_NAME = "agent_qa_rate_limited_total"
_AUDIT_ENTRIES_NAME = "agent_qa_audit_entries"
_AUDIT_DROPPED_NAME = "agent_qa_audit_dropped_total"
_IDEMPOTENCY_RESULTS = frozenset({"stored", "replayed", "mismatch", "in_progress"})
_RATE_LIMIT_KINDS = frozenset({"key", "client", "ip"})
_HTTP_METHODS = frozenset(
    {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE", "CONNECT"}
)
_OTHER_METHOD = "OTHER"
MetricSample = tuple[str, tuple[tuple[str, str], ...], str]


def _escape_label(value: str) -> str:
    """Escape a Prometheus label value."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    values = ",".join(
        f'{name}="{_escape_label(value)}"' for name, value in sorted(labels)
    )
    return "{" + values + "}"


class MetricsRegistry:
    """Collect request counters and render a stable Prometheus snapshot."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str, str], int] = {}
        self._durations: dict[tuple[str, str], tuple[float, int]] = {}
        self._idempotency: dict[str, int] = {}
        self._rate_limited: dict[str, int] = {}

    def record(
        self, method: str, route: str, status: int, duration_seconds: float
    ) -> None:
        """Record one completed request and its elapsed time."""
        method_label = str(method).upper()
        if method_label not in _HTTP_METHODS:
            method_label = _OTHER_METHOD
        request_labels = (method_label, str(route), str(status))
        duration_labels = request_labels[:2]
        with self._lock:
            self._requests[request_labels] = self._requests.get(request_labels, 0) + 1
            duration_sum, duration_count = self._durations.get(
                duration_labels, (0.0, 0)
            )
            self._durations[duration_labels] = (
                duration_sum + float(duration_seconds),
                duration_count + 1,
            )

    def record_idempotency(self, result: str) -> None:
        """Record a completed idempotency decision using fixed result labels."""
        if result not in _IDEMPOTENCY_RESULTS:
            raise ValueError("unknown idempotency result")
        with self._lock:
            self._idempotency[result] = self._idempotency.get(result, 0) + 1

    def record_rate_limited(self, kind: str) -> None:
        """Record a rate-limit decision with a bounded identity-kind label."""
        if kind not in _RATE_LIMIT_KINDS:
            raise ValueError("unknown rate limit identity kind")
        with self._lock:
            self._rate_limited[kind] = self._rate_limited.get(kind, 0) + 1

    def render(
        self,
        orders: int,
        git_sha: str,
        products: int | None = None,
        *,
        audit_entries: int | None = None,
        audit_dropped: int | None = None,
    ) -> str:
        """Render metrics from a request snapshot and current service values."""
        with self._lock:
            requests = self._requests.copy()
            durations = self._durations.copy()
            idempotency = self._idempotency.copy()
            rate_limited = self._rate_limited.copy()

        families: list[tuple[str, str, str, list[MetricSample]]] = [
            (
                _BUILD_NAME,
                "Build information.",
                "gauge",
                [(_BUILD_NAME, (("git_sha", str(git_sha)),), "1")],
            ),
            (
                _DURATION_NAME,
                "HTTP request duration in seconds.",
                "summary",
                [
                    (
                        f"{_DURATION_NAME}_{suffix}",
                        (("method", labels[0]), ("route", labels[1])),
                        str(value),
                    )
                    for labels, (duration_sum, duration_count) in durations.items()
                    for suffix, value in (
                        ("count", duration_count),
                        ("sum", duration_sum),
                    )
                ],
            ),
            (
                _REQUESTS_NAME,
                "Total HTTP requests.",
                "counter",
                [
                    (
                        _REQUESTS_NAME,
                        (
                            ("method", labels[0]),
                            ("route", labels[1]),
                            ("status", labels[2]),
                        ),
                        str(value),
                    )
                    for labels, value in requests.items()
                ],
            ),
            (
                _ORDERS_NAME,
                "Current number of orders.",
                "gauge",
                [(_ORDERS_NAME, (), str(orders))],
            ),
        ]
        if products is not None:
            families.append(
                (
                    _PRODUCTS_NAME,
                    "Current number of products.",
                    "gauge",
                    [(_PRODUCTS_NAME, (), str(products))],
                )
            )
        if idempotency:
            families.append(
                (
                    _IDEMPOTENCY_NAME,
                    "Total idempotency request outcomes.",
                    "counter",
                    [
                        (_IDEMPOTENCY_NAME, (("result", result),), str(value))
                        for result, value in idempotency.items()
                    ],
                )
            )
        if rate_limited:
            families.append(
                (
                    _RATE_LIMITED_NAME,
                    "Total requests denied by rate limiting.",
                    "counter",
                    [
                        (_RATE_LIMITED_NAME, (("kind", kind),), str(value))
                        for kind, value in rate_limited.items()
                    ],
                )
            )
        if audit_entries is not None:
            families.append(
                (
                    _AUDIT_ENTRIES_NAME,
                    "Current number of retained audit entries.",
                    "gauge",
                    [(_AUDIT_ENTRIES_NAME, (), str(max(0, int(audit_entries))))],
                )
            )
        if audit_dropped is not None:
            families.append(
                (
                    _AUDIT_DROPPED_NAME,
                    "Total audit entries dropped from the ring buffer.",
                    "counter",
                    [(_AUDIT_DROPPED_NAME, (), str(max(0, int(audit_dropped))))],
                )
            )

        lines: list[str] = []
        for name, help_text, metric_type, samples in sorted(families):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {metric_type}")
            for sample_name, labels, value in sorted(samples):
                lines.append(f"{sample_name}{_format_labels(labels)} {value}")
        return "\n".join(lines) + "\n"


REGISTRY = MetricsRegistry()
