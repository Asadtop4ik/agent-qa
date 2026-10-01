"""In-memory webhook configuration and simulated delivery outbox."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import math
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .errors import ApiError


_LOG = logging.getLogger(__name__)
_EVENT_TYPES = (
    "order.created",
    "order.updated",
    "order.deleted",
    "product.created",
    "product.updated",
    "product.deleted",
)
_EVENT_FILTERS = frozenset((*_EVENT_TYPES, "order.*", "product.*", "*"))
_STATUSES = ("pending", "retrying", "delivered", "failed")
_MAX_WEBHOOKS = 20
_MAX_CAPACITY = 500
_MAX_ID = 2**31 - 1
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.IGNORECASE)


def _bad_request(code: str, message: str, field: str | None = None) -> ApiError:
    details = [{"field": field, "message": message}] if field else None
    return ApiError(400, code, message, details)


def _validate_events(value: object) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= len(_EVENT_FILTERS):
        raise _bad_request(
            "invalid_events", "events must be a non-empty list", "events"
        )
    if any(
        not isinstance(event, str) or event not in _EVENT_FILTERS for event in value
    ):
        raise _bad_request(
            "invalid_events", "events contains an unknown event", "events"
        )
    if len(set(value)) != len(value):
        raise _bad_request(
            "invalid_events", "events must not contain duplicates", "events"
        )
    return list(value)


def _validate_url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise _bad_request("invalid_url", "url must be at most 200 characters", "url")
    if any(ord(character) < 32 or character.isspace() for character in value):
        raise _bad_request("invalid_url", "url must be a valid HTTP URL", "url")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise _bad_request(
            "invalid_url", "url must be a valid HTTP URL", "url"
        ) from exc
    valid_hostname = bool(
        hostname
        and len(hostname) <= 253
        and hostname.endswith(".invalid")
        and all(
            len(label) <= 63 and _DNS_LABEL.fullmatch(label)
            for label in hostname.split(".")
        )
    )
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not valid_hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise _bad_request(
            "invalid_url", "url must use http(s) and a .invalid host", "url"
        )
    return value


def _bounded_integer(value: object, minimum: int, maximum: int, field: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _bad_request("invalid_value", f"{field} is out of range", field)
    return value


def _epoch(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("clock must return a finite epoch time")
    result = float(value)
    if not math.isfinite(result) or result < 0 or result > 4_102_444_800:
        raise ValueError("clock must return a bounded epoch time")
    return result


def _iso(value: float) -> str:
    return (
        datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    )


class OutboxStore:
    """Thread-safe, bounded configuration and outbox store."""

    def __init__(
        self,
        *,
        clock=time.time,
        capacity: int = _MAX_CAPACITY,
        start_dispatcher: bool = True,
    ) -> None:
        self._clock = clock
        self._capacity = _bounded_integer(capacity, 1, _MAX_CAPACITY, "capacity")
        self._start_dispatcher = start_dispatcher
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stopping = False
        self._dispatcher_thread: threading.Thread | None = None
        self._webhooks: dict[int, dict[str, object]] = {}
        self._entries: dict[int, dict[str, object]] = {}
        self._next_webhook_id = 1
        self._next_entry_id = 1
        self._next_event_id = 1
        self._dropped = 0
        self._dispatcher = {"enabled": True, "interval_ms": 1000}

    def _now(self) -> float:
        return _epoch(self._clock())

    def _ensure_dispatcher(self) -> None:
        if not self._start_dispatcher:
            return
        with self._lock:
            if not self._dispatcher["enabled"] or self._stopping:
                return
            if self._dispatcher_thread and self._dispatcher_thread.is_alive():
                self._wake.set()
                return
            self._dispatcher_thread = threading.Thread(
                target=self._dispatch_loop,
                name="agent-qa-outbox-dispatcher",
                daemon=True,
            )
            self._dispatcher_thread.start()

    def _dispatch_loop(self) -> None:
        while True:
            with self._lock:
                if self._stopping:
                    return
                enabled = bool(self._dispatcher["enabled"])
                delay = int(self._dispatcher["interval_ms"]) / 1000
                self._wake.clear()
            self._wake.wait(delay)
            with self._lock:
                if self._stopping:
                    return
                enabled = bool(self._dispatcher["enabled"])
            if enabled:
                try:
                    self.process_due(now=self._now())
                except Exception:
                    _LOG.exception(
                        "outbox dispatcher failed while processing due entries"
                    )

    def stop(self, timeout: float = 1.0) -> None:
        """Stop the optional daemon dispatcher and wait briefly for it to exit."""
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise ValueError("timeout must be numeric")
        timeout = min(max(float(timeout), 0), 10)
        with self._lock:
            self._stopping = True
            thread = self._dispatcher_thread
            self._wake.set()
        if thread and thread is not threading.current_thread():
            thread.join(timeout)

    def create_webhook(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise _bad_request("invalid_webhook", "webhook body must be an object")
        if set(payload) - {
            "url",
            "events",
            "secret",
            "max_attempts",
            "backoff_base_ms",
        }:
            raise _bad_request("invalid_webhook", "unsupported webhook field")
        url = _validate_url(payload.get("url"))
        events = _validate_events(payload.get("events"))
        secret = payload.get("secret")
        if not isinstance(secret, str) or not 8 <= len(secret) <= 64:
            raise _bad_request(
                "invalid_secret", "secret must be 8 to 64 characters", "secret"
            )
        try:
            secret.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _bad_request(
                "invalid_secret", "secret must be valid UTF-8", "secret"
            ) from exc
        max_attempts = _bounded_integer(
            payload.get("max_attempts", 5), 1, 8, "max_attempts"
        )
        backoff = _bounded_integer(
            payload.get("backoff_base_ms", 1000), 0, 60000, "backoff_base_ms"
        )
        with self._lock:
            if len(self._webhooks) >= _MAX_WEBHOOKS:
                raise ApiError(409, "webhook_limit", "webhook limit reached")
            identifier = self._next_webhook_id
            self._next_webhook_id += 1
            webhook = {
                "id": identifier,
                "url": url,
                "events": events,
                "active": True,
                "secret": secret,
                "max_attempts": max_attempts,
                "backoff_base_ms": backoff,
                "created_at": _iso(self._now()),
            }
            self._webhooks[identifier] = webhook
            result = self._public_webhook(webhook)
        self._ensure_dispatcher()
        return result

    @staticmethod
    def _public_webhook(webhook: dict[str, object]) -> dict[str, object]:
        return {
            "id": webhook["id"],
            "url": webhook["url"],
            "events": list(webhook["events"]),
            "active": webhook["active"],
            "max_attempts": webhook["max_attempts"],
            "backoff_base_ms": webhook["backoff_base_ms"],
            "secret_set": True,
            "created_at": webhook["created_at"],
        }

    def list_webhooks(self) -> dict[str, object]:
        with self._lock:
            items = [self._public_webhook(item) for item in self._webhooks.values()]
        return {"items": items, "total": len(items)}

    def get_webhook(self, webhook_id: object) -> dict[str, object]:
        identifier = self._parse_id(webhook_id, "webhook_id")
        with self._lock:
            webhook = self._webhooks.get(identifier)
            if webhook is None:
                raise ApiError(404, "webhook_not_found", "webhook not found")
            return self._public_webhook(webhook)

    def patch_webhook(self, webhook_id: object, payload: object) -> dict[str, object]:
        identifier = self._parse_id(webhook_id, "webhook_id")
        if not isinstance(payload, dict) or not payload:
            raise _bad_request(
                "invalid_webhook", "patch body must be a non-empty object"
            )
        unknown = set(payload) - {"active", "events"}
        if unknown:
            raise _bad_request("invalid_webhook", "unsupported webhook field")
        active = payload.get("active")
        if "active" in payload and type(active) is not bool:
            raise _bad_request("invalid_value", "active must be a boolean", "active")
        events = _validate_events(payload["events"]) if "events" in payload else None
        with self._lock:
            webhook = self._webhooks.get(identifier)
            if webhook is None:
                raise ApiError(404, "webhook_not_found", "webhook not found")
            if "active" in payload:
                webhook["active"] = active
            if events is not None:
                webhook["events"] = events
            result = self._public_webhook(webhook)
        self._ensure_dispatcher()
        return result

    def delete_webhook(self, webhook_id: object) -> None:
        identifier = self._parse_id(webhook_id, "webhook_id")
        with self._lock:
            if self._webhooks.pop(identifier, None) is None:
                raise ApiError(404, "webhook_not_found", "webhook not found")
            for entry in self._entries.values():
                if entry["webhook_id"] == identifier and entry["status"] in {
                    "pending",
                    "retrying",
                }:
                    entry["status"] = "failed"
                    entry["next_attempt_at"] = None
                    entry["_next_attempt_epoch"] = None

    def emit(self, event_type: str, data: object) -> None:
        if event_type not in _EVENT_TYPES:
            raise _bad_request("invalid_event", "unknown event type", "event_type")
        try:
            copy.deepcopy(data)
            json.dumps(data, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError, RecursionError) as exc:
            raise _bad_request(
                "invalid_event", "event data must be JSON-compatible"
            ) from exc
        with self._lock:
            event_id = self._next_event_id
            self._next_event_id += 1
            event = {
                "id": f"evt_{event_id}",
                "type": event_type,
                "created_at": _iso(self._now()),
                "data": copy.deepcopy(data),
            }
            matched = [
                webhook
                for webhook in self._webhooks.values()
                if webhook["active"] and self._matches(webhook["events"], event_type)
            ]
            for webhook in matched:
                self._make_room()
                if len(self._entries) >= self._capacity:
                    self._dropped += 1
                    continue
                identifier = self._next_entry_id
                self._next_entry_id += 1
                self._entries[identifier] = {
                    "id": identifier,
                    "webhook_id": webhook["id"],
                    "event_id": event["id"],
                    "event_type": event_type,
                    "status": "pending",
                    "attempts": [],
                    "next_attempt_at": None,
                    "created_at": event["created_at"],
                    "payload": copy.deepcopy(event),
                    "_next_attempt_epoch": None,
                    "_cycle_attempts": 0,
                }
        if matched:
            self._ensure_dispatcher()

    @staticmethod
    def _matches(filters: object, event_type: str) -> bool:
        topic_filter = event_type.split(".")[0] + ".*"
        return "*" in filters or event_type in filters or topic_filter in filters

    def _make_room(self) -> None:
        if len(self._entries) < self._capacity:
            return
        terminal_ids = [
            identifier
            for identifier, item in self._entries.items()
            if item["status"] in {"delivered", "failed"}
        ]
        if terminal_ids:
            del self._entries[min(terminal_ids)]

    def list_outbox(self, query: list[tuple[str, str]]) -> dict[str, object]:
        if not isinstance(query, list) or len(query) > 20:
            raise _bad_request("invalid_query", "query is invalid")
        filters: dict[str, str] = {}
        allowed = {"status", "webhook_id", "event_type", "limit", "offset"}
        for pair in query:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise _bad_request("invalid_query", "query is invalid")
            key, value = pair
            if (
                key not in allowed
                or key in filters
                or not isinstance(value, str)
                or len(value) > 64
            ):
                field = key if isinstance(key, str) and key in allowed else None
                raise _bad_request("invalid_query", "query is invalid", field)
            filters[key] = value
        status = filters.get("status")
        if status is not None and status not in _STATUSES:
            raise _bad_request("invalid_query", "unknown status", "status")
        webhook_id = (
            self._parse_id(filters["webhook_id"], "webhook_id")
            if "webhook_id" in filters
            else None
        )
        event_type = filters.get("event_type")
        if event_type is not None and event_type not in _EVENT_TYPES:
            raise _bad_request("invalid_query", "unknown event type", "event_type")
        limit = self._query_integer(filters.get("limit", "50"), 1, 100, "limit")
        offset = self._query_integer(filters.get("offset", "0"), 0, _MAX_ID, "offset")
        with self._lock:
            rows = [
                row
                for row in self._entries.values()
                if (status is None or row["status"] == status)
                and (webhook_id is None or row["webhook_id"] == webhook_id)
                and (event_type is None or row["event_type"] == event_type)
            ]
            rows.sort(key=lambda row: row["id"])
            result = [self._public_entry(row) for row in rows[offset : offset + limit]]
            return {
                "items": result,
                "total": len(rows),
                "limit": limit,
                "offset": offset,
            }

    @staticmethod
    def _query_integer(value: str, minimum: int, maximum: int, field: str) -> int:
        if not value.isascii() or not value.isdecimal() or len(value) > 10:
            raise _bad_request("invalid_query", f"{field} is invalid", field)
        return _bounded_integer(int(value), minimum, maximum, field)

    @staticmethod
    def _parse_id(value: object, field: str) -> int:
        if type(value) is int:
            identifier = value
        elif (
            isinstance(value, str)
            and value.isascii()
            and value.isdecimal()
            and len(value) <= 10
        ):
            identifier = int(value)
        else:
            raise _bad_request("invalid_id", f"{field} is invalid", field)
        if not 1 <= identifier <= _MAX_ID:
            raise _bad_request("invalid_id", f"{field} is invalid", field)
        return identifier

    @staticmethod
    def _public_entry(entry: dict[str, object]) -> dict[str, object]:
        return {
            key: copy.deepcopy(entry[key])
            for key in (
                "id",
                "webhook_id",
                "event_id",
                "event_type",
                "status",
                "attempts",
                "next_attempt_at",
                "created_at",
                "payload",
            )
        }

    def get_outbox(self, entry_id: object) -> dict[str, object]:
        identifier = self._parse_id(entry_id, "id")
        with self._lock:
            entry = self._entries.get(identifier)
            if entry is None:
                raise ApiError(404, "outbox_not_found", "outbox entry not found")
            return self._public_entry(entry)

    def process_due(
        self,
        now: object = None,
        *,
        ignore_schedule: bool = False,
        max_items: int = 50,
    ) -> dict[str, int]:
        if type(ignore_schedule) is not bool:
            raise _bad_request(
                "invalid_value",
                "ignore_schedule must be a boolean",
                "ignore_schedule",
            )
        _bounded_integer(max_items, 1, 100, "max")
        current = self._now() if now is None else _epoch(now)
        with self._lock:
            due = [
                entry["id"]
                for entry in self._entries.values()
                if entry["status"] in {"pending", "retrying"}
                and (
                    ignore_schedule
                    or entry["_next_attempt_epoch"] is None
                    or entry["_next_attempt_epoch"] <= current
                )
            ][:max_items]
        counts = {"processed": 0, "delivered": 0, "retrying": 0, "failed": 0}
        for identifier in due:
            with self._lock:
                entry = self._entries.get(identifier)
                if entry is None or entry["status"] not in {"pending", "retrying"}:
                    continue
                if (
                    not ignore_schedule
                    and entry["_next_attempt_epoch"] is not None
                    and entry["_next_attempt_epoch"] > current
                ):
                    continue
                webhook = self._webhooks.get(entry["webhook_id"])
                if webhook is None:
                    entry["status"] = "failed"
                    entry["next_attempt_at"] = None
                    entry["_next_attempt_epoch"] = None
                    counts["processed"] += 1
                    counts["failed"] += 1
                    continue
                n = int(entry["_cycle_attempts"]) + 1
                timestamp = int(current)
                canonical = json.dumps(
                    entry["payload"], sort_keys=True, separators=(",", ":")
                )
                signature = (
                    "sha256="
                    + hmac.new(
                        str(webhook["secret"]).encode(),
                        f"{timestamp}.{canonical}".encode(),
                        hashlib.sha256,
                    ).hexdigest()
                )
                outcome, http_status = self._simulate(
                    webhook["url"], len(entry["attempts"])
                )
                attempt = {
                    "n": n,
                    "at": _iso(current),
                    "outcome": outcome,
                    "http_status": http_status,
                    "timestamp": timestamp,
                    "signature": signature,
                }
                entry["attempts"].append(attempt)
                entry["_cycle_attempts"] = n
                counts["processed"] += 1
                if http_status == 200:
                    entry["status"] = "delivered"
                    entry["next_attempt_at"] = None
                    entry["_next_attempt_epoch"] = None
                    counts["delivered"] += 1
                    continue
                retryable = (
                    http_status is None
                    or http_status in {408, 429}
                    or http_status >= 500
                    or http_status < 400
                )
                attempts_limit = int(webhook["max_attempts"])
                if not retryable or n >= attempts_limit:
                    entry["status"] = "failed"
                    entry["next_attempt_at"] = None
                    entry["_next_attempt_epoch"] = None
                    counts["failed"] += 1
                    continue
                backoff_ms = min(
                    int(webhook["backoff_base_ms"]) * (2 ** (n - 1)), 60000
                )
                next_epoch = current + backoff_ms / 1000
                entry["status"] = "retrying"
                entry["_next_attempt_epoch"] = next_epoch
                entry["next_attempt_at"] = _iso(next_epoch)
                counts["retrying"] += 1
        return counts

    @staticmethod
    def _simulate(url: object, prior_attempts: int) -> tuple[str, int | None]:
        hostname = urlsplit(str(url)).hostname or ""
        label = hostname.split(".", 1)[0].lower()
        if label == "fail":
            return "http_500", 500
        if label == "flaky" and prior_attempts < 2:
            return "http_503", 503
        if label == "gone":
            return "http_410", 410
        if label == "slow":
            return "timeout", None
        return "http_200", 200

    def requeue(self, entry_id: object) -> dict[str, object]:
        identifier = self._parse_id(entry_id, "id")
        with self._lock:
            entry = self._entries.get(identifier)
            if entry is None:
                raise ApiError(404, "outbox_not_found", "outbox entry not found")
            if entry["status"] != "failed":
                raise ApiError(
                    409, "not_requeueable", "only failed entries can be requeued"
                )
            entry["status"] = "pending"
            entry["next_attempt_at"] = None
            entry["_next_attempt_epoch"] = None
            entry["_cycle_attempts"] = 0
            result = self._public_entry(entry)
        self._ensure_dispatcher()
        return result

    def get_dispatcher(self) -> dict[str, object]:
        with self._lock:
            return dict(self._dispatcher)

    def configure_dispatcher(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict) or not payload:
            raise _bad_request(
                "invalid_dispatcher", "dispatcher body must be a non-empty object"
            )
        if set(payload) - {"enabled", "interval_ms"}:
            raise _bad_request("invalid_dispatcher", "unsupported dispatcher field")
        enabled = payload.get("enabled", self._dispatcher["enabled"])
        interval = payload.get("interval_ms", self._dispatcher["interval_ms"])
        if type(enabled) is not bool:
            raise _bad_request("invalid_value", "enabled must be a boolean", "enabled")
        interval = _bounded_integer(interval, 100, 60000, "interval_ms")
        with self._lock:
            self._dispatcher = {"enabled": enabled, "interval_ms": interval}
            result = dict(self._dispatcher)
            self._wake.set()
        if enabled:
            self._ensure_dispatcher()
        return result

    def metrics_snapshot(self) -> tuple[dict[str, int], int]:
        with self._lock:
            counts = {status: 0 for status in _STATUSES}
            for entry in self._entries.values():
                counts[entry["status"]] += 1
            return counts, self._dropped


OUTBOX = OutboxStore()
