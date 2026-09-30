"""In-memory order storage and HTTP-independent validation."""

from __future__ import annotations

from datetime import datetime, timezone
import threading
from typing import Any

from agent_qa.errors import ApiError
from agent_qa.pagination import (
    MAX_CURSOR_LENGTH,
    decode_cursor,
    encode_cursor,
    filter_fingerprint,
    keyset_page,
)
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


def _store_query_error(field: str, message: str) -> ApiError:
    return ApiError(
        400,
        "invalid_query",
        "Invalid query parameters",
        [{"field": field, "message": message}],
    )


def validate_create(payload: Any) -> dict[str, Any]:
    """Validate and return normalized fields for creating an order."""
    candidate = payload if isinstance(payload, dict) else {}
    errors = validate(SCHEMAS["CreateOrder"], candidate)
    if errors:
        raise _validation_error(errors)
    fields = {"customer_id": candidate["customer_id"]}
    if "items" in candidate:
        fields["items"] = [dict(item) for item in candidate["items"]]
    else:
        fields["total_cents"] = candidate["total_cents"]
    return fields


def validate_patch(payload: Any) -> dict[str, Any]:
    """Validate and return normalized fields for updating an order."""
    errors = validate(SCHEMAS["UpdateOrder"], payload)
    if errors:
        raise _validation_error(errors)
    return dict(payload)


def validate_query(query: list[tuple[str, str]]) -> dict[str, Any]:
    """Validate order-list query pairs and return their effective values."""
    allowed = {
        "status",
        "customer_id",
        "limit",
        "offset",
        "sort",
        "pagination",
        "cursor",
    }
    values: dict[str, str] = {}
    errors: list[dict[str, str]] = []
    invalid_cursor_input = False
    pairs = iter(query) if isinstance(query, (list, tuple)) else iter(())
    for index, pair in enumerate(pairs):
        if index >= 1000:
            errors.append({"field": "query", "message": "Too many query parameters"})
            break
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            errors.append({"field": "query", "message": "Invalid query parameter"})
            continue
        name, value = pair
        if not isinstance(name, str) or not isinstance(value, str):
            errors.append(
                {"field": str(name)[:128], "message": "Invalid query parameter"}
            )
            continue
        if name == "cursor" and len(value) > MAX_CURSOR_LENGTH:
            invalid_cursor_input = True
            if name in values:
                errors.append({"field": name, "message": "Parameter may appear once"})
            else:
                values[name] = ""
            continue
        if len(name) > 128 or len(value) > 4096:
            errors.append(
                {"field": name[:128], "message": "Query parameter is too long"}
            )
            continue
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
    if "customer_id" in values and (
        not MIN_CUSTOMER_ID_LENGTH
        <= len(values["customer_id"])
        <= MAX_CUSTOMER_ID_LENGTH
        or not values["customer_id"].strip()
    ):
        errors.append({"field": "customer_id", "message": "Invalid customer ID"})
    if "sort" in values and values["sort"] not in {"id", "-id"}:
        errors.append({"field": "sort", "message": "Unsupported sort order"})
    if "pagination" in values and values["pagination"] not in {"offset", "cursor"}:
        errors.append({"field": "pagination", "message": "Must be offset or cursor"})
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

    cursor_mode = values.get("pagination") == "cursor" or "cursor" in values
    if cursor_mode and "offset" in values:
        errors.append(
            {"field": "cursor", "message": "Offset cannot be used in cursor mode"}
        )
    elif "cursor" in values and values.get("pagination") == "offset":
        errors.append(
            {"field": "cursor", "message": "Cursor cannot be combined with offset"}
        )

    if errors:
        errors.sort(key=lambda item: item["field"])
        raise OrderError("invalid_query", "Invalid query parameters", errors)
    if invalid_cursor_input:
        raise ApiError(400, "invalid_cursor", "Invalid cursor")
    result: dict[str, Any] = {
        "limit": limit,
        "sort": values.get("sort", "id"),
        "pagination": "cursor" if cursor_mode else "offset",
    }
    if not cursor_mode:
        result["offset"] = offset
    if "cursor" in values:
        result["cursor"] = values["cursor"]
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
        self._lock = threading.RLock()

    @staticmethod
    def _copy_order(order: dict[str, Any]) -> dict[str, Any]:
        return {**order, "items": [dict(item) for item in order.get("items", [])]}

    def create(self, customer_id: str, total_cents: int) -> dict[str, Any]:
        fields = validate_create(
            {"customer_id": customer_id, "total_cents": total_cents}
        )
        with self._lock:
            return self._create_locked({**fields, "items": []})

    def _create_locked(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Create a previously validated order while ``_lock`` is held."""
        if len(self._orders) >= self._capacity:
            raise OrderError("store_full", "Order store is full")
        order_id = self._next_id
        self._next_id += 1
        order = {
            "id": order_id,
            **fields,
            "items": [dict(item) for item in fields.get("items", [])],
            "status": "new",
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        self._orders[order_id] = order
        return self._copy_order(order)

    def list(
        self,
        status: str | None = None,
        customer_id: str | None = None,
        limit: int = 20,
        offset: int | None = None,
        sort: str = "id",
        pagination: str | None = None,
        cursor: str | None = None,
    ) -> (
        tuple[list[dict[str, Any]], int] | tuple[list[dict[str, Any]], int, str | None]
    ):
        offset_supplied = offset is not None
        if sort not in {"id", "-id"}:
            raise _store_query_error("sort", "Unsupported sort order")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not MIN_LIMIT <= limit <= MAX_LIMIT
        ):
            raise _store_query_error("limit", "Invalid limit")
        if offset is None:
            offset = DEFAULT_OFFSET
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < MIN_OFFSET
        ):
            raise _store_query_error("offset", "Invalid offset")
        explicit_pagination = pagination is not None
        pagination = pagination or ("cursor" if cursor is not None else "offset")
        cursor_mode = pagination == "cursor" or cursor is not None
        if (
            pagination not in {"offset", "cursor"}
            or (cursor_mode and offset_supplied)
            or (explicit_pagination and pagination == "offset" and cursor is not None)
        ):
            raise _store_query_error("cursor", "Cursor cannot be combined with offset")
        filters = {"status": status, "customer_id": customer_id}
        fingerprint = filter_fingerprint(filters)
        cursor_key = (
            decode_cursor(cursor, sort, fingerprint) if cursor is not None else None
        )
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
            if cursor_mode:
                page, next_key = keyset_page(matched, sort, cursor_key, limit)
                items = [self._copy_order(order) for order in page]
                next_cursor = (
                    encode_cursor(sort, fingerprint, next_key)
                    if next_key is not None
                    else None
                )
                return items, total, next_cursor
            if sort == "-id":
                matched.reverse()
            return (
                [self._copy_order(order) for order in matched[offset : offset + limit]],
                total,
            )

    def get(self, order_id: int) -> dict[str, Any] | None:
        if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id <= 0:
            return None
        with self._lock:
            order = self._orders.get(order_id)
            return self._copy_order(order) if order is not None else None

    def update(self, order_id: int, changes: dict[str, Any]) -> dict[str, Any] | None:
        fields = validate_patch(changes)
        with self._lock:
            current = self._orders.get(order_id)
            if current is None:
                return None
            if "total_cents" in fields and current.get("items"):
                from agent_qa.errors import ApiError

                raise ApiError(
                    409, "total_computed", "Order total is computed from items"
                )
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
            return self._copy_order(updated)

    def delete(self, order_id: int) -> bool:
        if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id <= 0:
            return False
        with self._lock:
            return self._orders.pop(order_id, None) is not None
