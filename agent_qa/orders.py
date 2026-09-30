"""In-memory order storage and HTTP-independent validation."""

from __future__ import annotations

from datetime import datetime, timezone
import threading
from typing import Any

from agent_qa.schemas import (
    DEFAULT_LIMIT,
    DEFAULT_OFFSET,
    MAX_CUSTOMER_ID_LENGTH as MAX_CUSTOMER_ID_LENGTH,
    MAX_LIMIT,
    MAX_ORDERS,
    MAX_TOTAL_CENTS as MAX_TOTAL_CENTS,
    MIN_CUSTOMER_ID_LENGTH as MIN_CUSTOMER_ID_LENGTH,
    MIN_LIMIT,
    MIN_OFFSET,
    MIN_TOTAL_CENTS as MIN_TOTAL_CENTS,
    ORDER_STATUSES,
    SCHEMAS,
    STATUSES as STATUSES,
)
from agent_qa.validation import validate


class OrderError(Exception):
    """Domain error raised by order validation and storage operations."""

    def __init__(
        self, code: str, message: str, details: list[dict[str, str]] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _validation_error(details: list[dict[str, str]]) -> OrderError:
    details.sort(key=lambda item: item["field"])
    return OrderError("validation_error", "Request validation failed", details)


def validate_create(payload: Any) -> dict[str, Any]:
    """Validate and return normalized fields for creating an order."""
    candidate = payload if isinstance(payload, dict) else {}
    errors = validate(SCHEMAS["CreateOrder"], candidate)
    if errors:
        raise _validation_error(errors)
    return {
        "customer_id": candidate["customer_id"],
        "total_cents": candidate["total_cents"],
    }


def validate_patch(payload: Any) -> dict[str, Any]:
    """Validate and return normalized fields for updating an order."""
    errors = validate(SCHEMAS["UpdateOrder"], payload)
    if errors:
        raise _validation_error(errors)
    return dict(payload)


def validate_query(query: list[tuple[str, str]]) -> dict[str, Any]:
    """Validate order-list query pairs and return their effective values."""
    allowed = {"status", "customer_id", "limit", "offset"}
    values: dict[str, str] = {}
    errors: list[dict[str, str]] = []
    for name, value in query:
        if name not in allowed:
            errors.append({"field": name, "message": "Unsupported query parameter"})
        elif name in values:
            errors.append({"field": name, "message": "Parameter may appear once"})
        else:
            values[name] = value

    if "status" in values and values["status"] not in ORDER_STATUSES:
        errors.append(
            {"field": "status", "message": "Must be a supported order status"}
        )
    if "limit" in values:
        try:
            limit = int(values["limit"])
            if not MIN_LIMIT <= limit <= MAX_LIMIT:
                raise ValueError
        except ValueError:
            errors.append(
                {
                    "field": "limit",
                    "message": (f"Must be an integer from {MIN_LIMIT} to {MAX_LIMIT}"),
                }
            )
    else:
        limit = DEFAULT_LIMIT
    if "offset" in values:
        try:
            offset = int(values["offset"])
            if offset < MIN_OFFSET:
                raise ValueError
        except ValueError:
            errors.append(
                {
                    "field": "offset",
                    "message": "Must be a non-negative integer",
                }
            )
    else:
        offset = DEFAULT_OFFSET

    if errors:
        errors.sort(key=lambda item: item["field"])
        raise OrderError("invalid_query", "Invalid query parameters", errors)
    result: dict[str, Any] = {"limit": limit, "offset": offset}
    if "status" in values:
        result["status"] = values["status"]
    if "customer_id" in values:
        result["customer_id"] = values["customer_id"]
    return result


class OrderStore:
    """Thread-safe, process-local order storage with never-reused IDs."""

    def __init__(self, capacity: int = MAX_ORDERS) -> None:
        if not 1 <= capacity <= MAX_ORDERS:
            raise ValueError(f"capacity must be between 1 and {MAX_ORDERS}")
        self._capacity = capacity
        self._orders: dict[int, dict[str, Any]] = {}
        self._next_id = 1
        self._lock = threading.Lock()

    def create(self, customer_id: str, total_cents: int) -> dict[str, Any]:
        fields = validate_create(
            {"customer_id": customer_id, "total_cents": total_cents}
        )
        with self._lock:
            if len(self._orders) >= self._capacity:
                raise OrderError("store_full", "Order store is full")
            order_id = self._next_id
            self._next_id += 1
            order = {
                "id": order_id,
                **fields,
                "status": "new",
                "created_at": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
            self._orders[order_id] = order
            return dict(order)

    def list(
        self,
        status: str | None = None,
        customer_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            matched = [
                self._orders[order_id]
                for order_id in sorted(self._orders)
                if (status is None or self._orders[order_id]["status"] == status)
                and (
                    customer_id is None
                    or self._orders[order_id]["customer_id"] == customer_id
                )
            ]
            total = len(matched)
            return [dict(order) for order in matched[offset : offset + limit]], total

    def get(self, order_id: int) -> dict[str, Any] | None:
        if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id <= 0:
            return None
        with self._lock:
            order = self._orders.get(order_id)
            return dict(order) if order is not None else None

    def update(self, order_id: int, changes: dict[str, Any]) -> dict[str, Any] | None:
        fields = validate_patch(changes)
        with self._lock:
            current = self._orders.get(order_id)
            if current is None:
                return None
            if "total_cents" in fields and current["status"] != "new":
                raise OrderError(
                    "order_locked", "Order total can only change while new"
                )
            if "status" in fields:
                allowed_transitions = {
                    "new": {"paid", "cancelled"},
                    "paid": {"shipped", "cancelled"},
                    "shipped": set(),
                    "cancelled": set(),
                }
                if fields["status"] not in allowed_transitions[current["status"]]:
                    raise OrderError(
                        "invalid_transition",
                        "Order status transition is not allowed",
                    )
            updated = {**current, **fields}
            self._orders[order_id] = updated
            return dict(updated)

    def delete(self, order_id: int) -> bool:
        if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id <= 0:
            return False
        with self._lock:
            return self._orders.pop(order_id, None) is not None
