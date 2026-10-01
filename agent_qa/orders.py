"""In-memory order storage and HTTP-independent validation."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import threading
from typing import Any

from agent_qa.bulk import run_transaction
from agent_qa.conditional import check_expected_version
from agent_qa.context import get_context
from agent_qa.pagination import (
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
    pairs = iter(query) if isinstance(query, (list, tuple)) else iter(())
    explicit_offset = False
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
        if len(name) > 4096 or (name != "cursor" and len(value) > 4096):
            errors.append(
                {"field": name[:4096], "message": "Query parameter is too long"}
            )
            continue
        if name not in allowed:
            errors.append({"field": name, "message": "Unsupported query parameter"})
        elif name in values:
            errors.append({"field": name, "message": "Parameter may appear once"})
        else:
            values[name] = value
            explicit_offset = explicit_offset or name == "offset"

    if "status" in values and values["status"] not in ORDER_STATUSES:
        errors.append(
            {"field": "status", "message": "Must be a supported order status"}
        )
    if "customer_id" in values and not (
        MIN_CUSTOMER_ID_LENGTH <= len(values["customer_id"]) <= MAX_CUSTOMER_ID_LENGTH
        and values["customer_id"].strip()
    ):
        errors.append({"field": "customer_id", "message": "Invalid customer ID"})
    if values.get("sort", "id") not in {"id", "-id"}:
        errors.append({"field": "sort", "message": "Unsupported sort order"})
    pagination = values.get("pagination", "offset")
    if pagination not in {"offset", "cursor"}:
        errors.append({"field": "pagination", "message": "Must be offset or cursor"})
    if ("cursor" in values or pagination == "cursor") and explicit_offset:
        errors.append(
            {"field": "cursor", "message": "Cannot combine cursor and offset"}
        )
    if "cursor" in values and values.get("pagination") == "offset":
        errors.append(
            {"field": "cursor", "message": "Cursor requires cursor pagination"}
        )
    if "limit" in values:
        try:
            if len(values["limit"]) > 10:
                raise ValueError
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
            if len(values["offset"]) > 19:
                raise ValueError
            offset = int(values["offset"])
            if offset < MIN_OFFSET or offset > (1 << 63) - 1:
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
    result: dict[str, Any] = {"limit": limit, "sort": values.get("sort", "id")}
    if pagination == "cursor" or "cursor" in values:
        result["pagination"] = "cursor"
        if "cursor" in values:
            result["cursor"] = values["cursor"]
    else:
        result["pagination"] = "offset"
        result["offset"] = offset
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
    def _copy_order(
        order: dict[str, Any], include_version: bool = False
    ) -> dict[str, Any]:
        copied = {**order, "items": [dict(item) for item in order.get("items", [])]}
        if not include_version:
            copied.pop("version", None)
        return copied

    def create(
        self, customer_id: str, total_cents: int, *, include_version: bool = False
    ) -> dict[str, Any]:
        fields = validate_create(
            {"customer_id": customer_id, "total_cents": total_cents}
        )
        with self._lock:
            return self._create_locked({**fields, "items": []}, include_version)

    def _create_locked(
        self, fields: dict[str, Any], include_version: bool = False
    ) -> dict[str, Any]:
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
            "version": 1,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        self._orders[order_id] = order
        return self._copy_order(order, include_version)

    def _snapshot_locked(self) -> tuple[dict[int, dict[str, Any]], int]:
        return deepcopy(self._orders), self._next_id

    def _restore_locked(self, snapshot: tuple[dict[int, dict[str, Any]], int]) -> None:
        orders, next_id = snapshot
        self._orders.clear()
        self._orders.update(deepcopy(orders))
        self._next_id = next_id

    def apply_csv_import(
        self,
        records: list[tuple[int, dict[str, Any]]],
        errors: list[dict[str, Any]],
        mode: str,
        on_error: str,
    ) -> tuple[int, int, list[dict[str, Any]]]:
        """Apply already validated legacy CSV rows under one store transaction."""
        with self._lock:
            row_errors = [dict(error) for error in errors]
            row_errors.sort(key=lambda item: item["line"])
            if mode == "validate":
                return 200, 0, row_errors
            if row_errors and on_error == "abort":
                return 422, 0, row_errors

            available = self._capacity - len(self._orders)
            if len(records) > available:
                for line, _fields in records[available:]:
                    row_errors.append(
                        {
                            "line": line,
                            "field": "body",
                            "message": "Order store is full",
                        }
                    )
                records = records[:available]
                row_errors.sort(key=lambda item: item["line"])
            if row_errors and on_error == "abort":
                return 422, 0, row_errors

            snapshot = self._snapshot_locked()

            def apply_rows() -> int:
                for _line, fields in records:
                    self._create_locked({**fields, "items": []})
                return len(records)

            created = run_transaction(
                apply_rows,
                lambda: self._restore_locked(snapshot),
                "order CSV import",
            )
            return (201 if created else 422), created, row_errors

    def list(
        self,
        status: str | None = None,
        customer_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
        sort: str = "id",
        pagination: str = "offset",
        cursor: str | None = None,
    ) -> (
        tuple[list[dict[str, Any]], int] | tuple[list[dict[str, Any]], int, str | None]
    ):
        if not isinstance(sort, str) or sort not in {"id", "-id"}:
            raise OrderError(
                "invalid_query",
                "Invalid query parameters",
                [{"field": "sort", "message": "Unsupported sort order"}],
            )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not MIN_LIMIT <= limit <= MAX_LIMIT
        ):
            raise OrderError(
                "invalid_query",
                "Invalid query parameters",
                [{"field": "limit", "message": "Invalid limit"}],
            )
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or not MIN_OFFSET <= offset <= (1 << 63) - 1
        ):
            raise OrderError(
                "invalid_query",
                "Invalid query parameters",
                [{"field": "offset", "message": "Invalid offset"}],
            )
        cursor_mode = pagination == "cursor" or cursor is not None
        if cursor_mode and offset != DEFAULT_OFFSET:
            raise OrderError(
                "invalid_query",
                "Invalid query parameters",
                [{"field": "cursor", "message": "Cannot combine cursor and offset"}],
            )
        if pagination not in {"offset", "cursor"}:
            raise OrderError(
                "invalid_query",
                "Invalid query parameters",
                [{"field": "pagination", "message": "Must be offset or cursor"}],
            )
        offset_sort_reverse = sort == "-id" and not cursor_mode
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
                filters = {"status": status, "customer_id": customer_id}
                fingerprint = filter_fingerprint(filters)
                cursor_key = (
                    decode_cursor(cursor, sort, fingerprint)
                    if cursor is not None
                    else None
                )
                page, has_more = keyset_page(
                    matched, ("id", sort == "-id"), cursor_key, limit
                )
                next_cursor = None
                if has_more and page:
                    last_id = page[-1]["id"]
                    next_cursor = encode_cursor(sort, fingerprint, [last_id, last_id])
                return [self._copy_order(order) for order in page], total, next_cursor
            if offset_sort_reverse:
                matched.reverse()
            return (
                [self._copy_order(order) for order in matched[offset : offset + limit]],
                total,
            )

    def export_csv(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Return up to 1000 orders matching the supported export filters."""
        filters = {} if filters is None else dict(filters)
        unsupported = filters.keys() - {"status", "customer_id"}
        if unsupported:
            field = sorted(unsupported)[0]
            raise OrderError(
                "invalid_query",
                "Invalid query parameters",
                [{"field": field, "message": "Unsupported query parameter"}],
            )
        with self._lock:
            rows = []
            for order_id in sorted(self._orders):
                order = self._orders[order_id]
                if (
                    filters.get("status") is not None
                    and order["status"] != filters["status"]
                ):
                    continue
                if (
                    filters.get("customer_id") is not None
                    and order["customer_id"] != filters["customer_id"]
                ):
                    continue
                rows.append(
                    {
                        "id": order["id"],
                        "customer_id": order["customer_id"],
                        "total_cents": order["total_cents"],
                        "status": order["status"],
                        "items_count": len(order.get("items", [])),
                        "created_at": order["created_at"],
                    }
                )
                if len(rows) == 1000:
                    break
            return rows

    def get(
        self, order_id: int, *, include_version: bool = False
    ) -> dict[str, Any] | None:
        if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id <= 0:
            return None
        with self._lock:
            order = self._orders.get(order_id)
            return (
                self._copy_order(order, include_version) if order is not None else None
            )

    def update(
        self,
        order_id: int,
        changes: dict[str, Any],
        *,
        expected_version: int | tuple[str, ...] | str | None = None,
        include_version: bool = False,
    ) -> dict[str, Any] | None:
        fields = validate_patch(changes)
        with self._lock:
            current = self._orders.get(order_id)
            if current is None:
                return None
            check_expected_version(
                expected_version, "order", order_id, current["version"]
            )
            context = get_context()
            if context is not None:
                context.resource = "orders"
                context.resource_id = order_id
                context.changes = None
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
            updated = {**current, **fields, "version": current["version"] + 1}
            self._orders[order_id] = updated
            if context is not None:
                changed = {
                    name: {"from": current[name], "to": value}
                    for name, value in fields.items()
                    if current.get(name) != value
                }
                if changed:
                    context.changes = changed
            return self._copy_order(updated, include_version)

    def delete(
        self,
        order_id: int,
        *,
        expected_version: int | tuple[str, ...] | str | None = None,
    ) -> bool:
        if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id <= 0:
            return False
        with self._lock:
            current = self._orders.get(order_id)
            if current is None:
                return False
            check_expected_version(
                expected_version, "order", order_id, current["version"]
            )
            context = get_context()
            if context is not None:
                context.resource = "orders"
                context.resource_id = order_id
            del self._orders[order_id]
            return True
