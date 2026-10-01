"""Thread-safe bounded in-memory audit log."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import os
import threading

from agent_qa.context import RequestContext

_MIN_CAPACITY = 10
_MAX_CAPACITY = 5000
_MAX_TEXT = 256
_MAX_ACTOR = 128
_ALLOWED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_ALLOWED_RESOURCES = frozenset({"orders", "products", "keys", "jobs"})
_CHANGE_TYPES = {
    "status": str,
    "total_cents": int,
    "name": str,
    "category": str,
    "price_cents": int,
    "stock": int,
    "tags": list,
    "active": bool,
}


def _configured_capacity() -> int:
    raw = os.environ.get("AGENT_QA_AUDIT_CAPACITY", "500")
    try:
        # Reject pathological values before int conversion (also handles Python's
        # configurable integer string limit without surfacing an import error).
        digits = raw[1:] if raw.startswith(("+", "-")) else raw
        if len(raw) > 12 or not raw.isascii() or not digits or not digits.isdecimal():
            return 500
        value = int(raw)
    except (TypeError, ValueError):
        return 500
    return min(_MAX_CAPACITY, max(_MIN_CAPACITY, value))


def _bounded_text(value: object, maximum: int = _MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    return value[:maximum]


def _safe_changes(changes: object) -> dict[str, object] | None:
    """Copy only validated store diffs and bulk count summaries."""
    if not isinstance(changes, dict):
        return None
    result: dict[str, object] = {}
    for name, diff in list(changes.items())[:8]:
        if not isinstance(name, str):
            continue
        if name == "summary" and isinstance(diff, dict):
            summary = {
                key: value
                for key, value in diff.items()
                if key in {"total", "succeeded", "failed"}
                and isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value <= 100_000
            }
            if summary:
                result["summary"] = summary
            continue
        expected_type = _CHANGE_TYPES.get(name)
        if expected_type is None or not isinstance(diff, dict):
            continue
        if set(diff) != {"from", "to"}:
            continue
        before, after = diff["from"], diff["to"]
        if expected_type is int:
            valid = all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value <= 2**31 - 1
                for value in (before, after)
            )
        elif expected_type is str:
            valid = all(
                isinstance(value, str) and len(value) <= _MAX_TEXT
                for value in (before, after)
            )
        elif expected_type is bool:
            valid = all(isinstance(value, bool) for value in (before, after))
        else:
            valid = all(
                isinstance(value, list)
                and len(value) <= 32
                and all(
                    isinstance(item, str) and len(item) <= _MAX_TEXT for item in value
                )
                for value in (before, after)
            )
        if valid and before != after:
            result[name] = {"from": deepcopy(before), "to": deepcopy(after)}
    return result or None


class AuditLog:
    """A bounded FIFO ring buffer with a monotonically increasing sequence."""

    def __init__(self, capacity: int | None = None) -> None:
        if capacity is None:
            capacity = _configured_capacity()
        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise ValueError("capacity must be an integer")
        self.capacity = min(_MAX_CAPACITY, max(_MIN_CAPACITY, capacity))
        self._entries: deque[dict[str, object]] = deque(maxlen=self.capacity)
        self._lock = threading.Lock()
        self._last_seq = 0
        self._dropped = 0

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._last_seq

    def append(
        self,
        context: RequestContext,
        method: str,
        route: str,
        path: str,
        status: int,
        *,
        replay: bool = False,
    ) -> dict[str, object]:
        """Append one completed write request without retaining request data."""
        method = str(method).upper()
        if method not in _ALLOWED_METHODS:
            raise ValueError("audit method must be a write method")
        if (
            isinstance(status, bool)
            or not isinstance(status, int)
            or not 100 <= status <= 599
        ):
            raise ValueError("invalid HTTP status")
        if not isinstance(context, RequestContext):
            raise TypeError("context must be a RequestContext")
        outcome = "success" if 200 <= status < 300 else "error"
        if status in {401, 403, 429}:
            outcome = "denied"
        elif 400 <= status < 500:
            outcome = "rejected"
        actor = context.actor if isinstance(context.actor, str) else "anonymous"
        if actor != "anonymous":
            actor = _bounded_text(actor, _MAX_ACTOR)
        role = (
            context.role
            if isinstance(context.role, str)
            and context.role in {"read", "write", "admin"}
            else None
        )
        resource = (
            context.resource
            if isinstance(context.resource, str)
            and context.resource in _ALLOWED_RESOURCES
            else None
        )
        resource_id: int | str | None = context.resource_id
        if isinstance(resource_id, bool) or not isinstance(resource_id, (int, str)):
            resource_id = None
        elif isinstance(resource_id, str):
            resource_id = _bounded_text(resource_id)
        safe_path = _bounded_text(path.split("?", 1)[0], 65536)
        safe_route = _bounded_text(route, 256)
        with self._lock:
            if len(self._entries) == self.capacity:
                self._dropped += 1
            self._last_seq += 1
            entry: dict[str, object] = {
                "seq": self._last_seq,
                "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "actor": actor,
                "role": role,
                "method": method,
                "route": safe_route,
                "path": safe_path,
                "resource": resource,
                "resource_id": resource_id,
                "status": status,
                "outcome": outcome,
                "request_id": _bounded_text(context.request_id, 64),
                "changes": _safe_changes(context.changes),
            }
            if replay:
                entry["replay"] = True
            self._entries.append(entry)
            return deepcopy(entry)

    def query(
        self,
        *,
        method: str | None = None,
        resource: str | None = None,
        resource_id: str | None = None,
        outcome: str | None = None,
        actor: str | None = None,
        status: int | None = None,
        since_seq: int = 0,
        limit: int = 50,
        order: str = "desc",
    ) -> dict[str, object]:
        """Return a filtered snapshot and buffer metadata."""
        if method is not None and (
            not isinstance(method, str) or method not in _ALLOWED_METHODS
        ):
            raise ValueError("invalid method")
        if resource is not None and (
            not isinstance(resource, str) or resource not in _ALLOWED_RESOURCES
        ):
            raise ValueError("invalid resource")
        if outcome is not None and (
            not isinstance(outcome, str)
            or outcome not in {"success", "denied", "rejected", "error"}
        ):
            raise ValueError("invalid outcome")
        if actor is not None and (
            not isinstance(actor, str) or len(actor) > _MAX_ACTOR
        ):
            raise ValueError("invalid actor")
        if resource_id is not None and (
            not isinstance(resource_id, str) or len(resource_id) > _MAX_TEXT
        ):
            raise ValueError("invalid resource_id")
        if status is not None and (
            isinstance(status, bool)
            or not isinstance(status, int)
            or not 100 <= status <= 599
        ):
            raise ValueError("invalid status")
        if (
            isinstance(since_seq, bool)
            or not isinstance(since_seq, int)
            or not 0 <= since_seq <= 2**63 - 1
        ):
            raise ValueError("invalid since_seq")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 200
        ):
            raise ValueError("invalid limit")
        if order not in {"asc", "desc"}:
            raise ValueError("invalid order")
        with self._lock:
            entries = deepcopy(list(self._entries))
            dropped = self._dropped
            last_seq = entries[-1]["seq"] if entries else 0
        matching = [
            entry
            for entry in entries
            if entry["seq"] > since_seq
            and (method is None or entry["method"] == method)
            and (resource is None or entry["resource"] == resource)
            and (resource_id is None or str(entry["resource_id"]) == resource_id)
            and (outcome is None or entry["outcome"] == outcome)
            and (actor is None or entry["actor"] == actor)
            and (status is None or entry["status"] == status)
        ]
        matching.sort(key=lambda entry: int(entry["seq"]), reverse=order == "desc")
        total_matching = len(matching)
        return {
            "items": matching[:limit],
            "total_matching": total_matching,
            "limit": limit,
            "capacity": self.capacity,
            "dropped": dropped,
            "last_seq": last_seq,
        }

    def get(self, seq: int) -> dict[str, object] | None:
        """Return a detached entry snapshot for a retained sequence number."""
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            return None
        with self._lock:
            for entry in self._entries:
                if entry["seq"] == seq:
                    return deepcopy(entry)
        return None

    def metrics_snapshot(self) -> tuple[int, int]:
        """Return retained-entry and dropped-entry counts from one lock snapshot."""
        with self._lock:
            return len(self._entries), self._dropped


AUDIT_LOG = AuditLog()
