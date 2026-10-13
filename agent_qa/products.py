"""Thread-safe in-memory product catalog and HTTP-independent validation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import threading
from typing import Any

from agent_qa.orders import OrderError
from agent_qa.schemas import (
    CATEGORY_PATTERN as CATEGORY_PATTERN,
    DEFAULT_LIMIT as DEFAULT_LIMIT,
    DEFAULT_OFFSET as DEFAULT_OFFSET,
    MAX_CATEGORY_LENGTH as MAX_CATEGORY_LENGTH,
    MAX_LIMIT as MAX_LIMIT,
    MAX_NAME_LENGTH as MAX_NAME_LENGTH,
    MAX_PRICE_CENTS as MAX_PRICE_CENTS,
    MAX_PRODUCTS as MAX_PRODUCTS,
    MAX_SKU_LENGTH as MAX_SKU_LENGTH,
    MAX_STOCK as MAX_STOCK,
    MAX_TAG_LENGTH as MAX_TAG_LENGTH,
    MAX_TAGS as MAX_TAGS,
    MIN_LIMIT as MIN_LIMIT,
    MIN_OFFSET as MIN_OFFSET,
    MIN_PRICE_CENTS as MIN_PRICE_CENTS,
    MIN_STOCK as MIN_STOCK,
    PRODUCT_SORTS as SORTS,
    SCHEMAS,
    SKU_PATTERN as SKU_PATTERN,
    TAG_PATTERN as TAG_PATTERN,
)
from agent_qa.validation import (
    MAX_QUERY_NAME_LENGTH,
    MAX_QUERY_PARAMETERS,
    MAX_QUERY_VALUE_LENGTH,
    validate,
)


class ProductError(OrderError):
    """Domain error raised by product validation and storage operations."""


def _validation_error(details: list[dict[str, str]]) -> ProductError:
    details.sort(key=lambda item: (item["message"] != "Unknown field", item["field"]))
    return ProductError("validation_error", "Request validation failed", details)


def _validated(schema_name: str, payload: Any) -> dict[str, Any]:
    errors = validate(SCHEMAS[schema_name], payload)
    if errors:
        raise _validation_error(errors)
    return dict(payload)


def validate_create(payload: Any) -> dict[str, Any]:
    """Validate a product creation object and fill its default values."""
    fields = _validated("CreateProduct", payload)
    fields.setdefault("stock", 0)
    fields.setdefault("tags", [])
    fields.setdefault("active", True)
    fields["tags"] = list(fields["tags"])
    return fields


def validate_patch(payload: Any) -> dict[str, Any]:
    """Validate product changes, including the SKU immutability rule."""
    fields = _validated("UpdateProduct", payload)
    if "tags" in fields:
        fields["tags"] = list(fields["tags"])
    return fields


def validate_adjust_stock(payload: Any) -> dict[str, Any]:
    """Validate a stock adjustment payload."""
    return _validated("AdjustStock", payload)


def _query_error(errors: list[dict[str, str]]) -> ProductError:
    errors.sort(key=lambda item: item["field"])
    return ProductError("invalid_query", "Invalid query parameters", errors)


def _query_integer(value: str, minimum: int, maximum: int | None) -> int:
    if not re.fullmatch(r"[+-]?[0-9]{1,128}", value):
        raise ValueError
    parsed = int(value)
    if parsed < minimum or (maximum is not None and parsed > maximum):
        raise ValueError
    return parsed


def validate_query(query: list[tuple[str, str]]) -> dict[str, Any]:
    """Validate product query pairs and return effective filter and page values."""
    allowed = {
        "category",
        "tag",
        "active",
        "in_stock",
        "min_price_cents",
        "max_price_cents",
        "q",
        "sort",
        "limit",
        "offset",
    }
    values: dict[str, str] = {}
    errors: list[dict[str, str]] = []
    if len(query) > MAX_QUERY_PARAMETERS:
        raise _query_error([{"field": "query", "message": "Too many query parameters"}])

    for name, value in query:
        if len(name) > MAX_QUERY_NAME_LENGTH:
            raise _query_error(
                [
                    {
                        "field": name[:MAX_QUERY_NAME_LENGTH],
                        "message": "Parameter name is too long",
                    }
                ]
            )
        if name not in allowed:
            errors.append({"field": name, "message": "Unsupported query parameter"})
        elif name in values:
            errors.append({"field": name, "message": "Parameter may appear once"})
        elif len(value) > MAX_QUERY_VALUE_LENGTH:
            errors.append({"field": name, "message": "Parameter value is too long"})
        else:
            values[name] = value

    for field, pattern in (("category", CATEGORY_PATTERN), ("tag", TAG_PATTERN)):
        if field in values and re.fullmatch(pattern, values[field]) is None:
            errors.append(
                {"field": field, "message": "Must match the required pattern"}
            )
    for field in ("active", "in_stock"):
        if field in values and values[field] not in {"true", "false"}:
            errors.append({"field": field, "message": "Must be a boolean"})
    if "q" in values and not 1 <= len(values["q"]) <= 64:
        errors.append({"field": "q", "message": "Must contain 1 to 64 characters"})
    if "sort" in values and values["sort"] not in SORTS:
        errors.append({"field": "sort", "message": "Must be a supported sort order"})

    parsed_ints: dict[str, int] = {}
    bounds = {
        "min_price_cents": (0, MAX_PRICE_CENTS),
        "max_price_cents": (0, MAX_PRICE_CENTS),
        "limit": (MIN_LIMIT, MAX_LIMIT),
        "offset": (MIN_OFFSET, None),
    }
    for field, (minimum, maximum) in bounds.items():
        if field not in values:
            continue
        try:
            parsed_ints[field] = _query_integer(values[field], minimum, maximum)
        except ValueError:
            if field == "limit":
                message = f"Must be an integer from {MIN_LIMIT} to {MAX_LIMIT}"
            elif maximum is not None:
                message = f"Must be an integer from {minimum} to {maximum}"
            else:
                message = "Must be a non-negative integer"
            errors.append({"field": field, "message": message})

    if (
        "min_price_cents" in parsed_ints
        and "max_price_cents" in parsed_ints
        and parsed_ints["min_price_cents"] > parsed_ints["max_price_cents"]
    ):
        errors.append(
            {"field": "min_price_cents", "message": "Must not exceed max_price_cents"}
        )
    if errors:
        raise _query_error(errors)

    result: dict[str, Any] = {
        "limit": parsed_ints.get("limit", DEFAULT_LIMIT),
        "offset": parsed_ints.get("offset", DEFAULT_OFFSET),
        "sort": values.get("sort", "id"),
    }
    result.update(parsed_ints)
    for field in ("category", "tag", "q"):
        if field in values:
            result[field] = values[field]
    for field in ("active", "in_stock"):
        if field in values:
            result[field] = values[field] == "true"
    return result


def _copy_product(product: dict[str, Any]) -> dict[str, Any]:
    copied = dict(product)
    copied["tags"] = list(product["tags"])
    return copied


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _valid_product_id(product_id: Any) -> bool:
    return (
        isinstance(product_id, int)
        and not isinstance(product_id, bool)
        and product_id > 0
    )


class ProductStore:
    """Thread-safe, bounded product storage with never-reused integer IDs."""

    def __init__(self, capacity: int = MAX_PRODUCTS) -> None:
        if (
            isinstance(capacity, bool)
            or not isinstance(capacity, int)
            or not 1 <= capacity <= MAX_PRODUCTS
        ):
            raise ValueError(f"capacity must be between 1 and {MAX_PRODUCTS}")
        self._capacity = capacity
        self._products: dict[int, dict[str, Any]] = {}
        self._sku_ids: dict[str, int] = {}
        self._next_id = 1
        self._lock = threading.Lock()

    def create(self, **payload: Any) -> dict[str, Any]:
        fields = validate_create(payload)
        with self._lock:
            if fields["sku"] in self._sku_ids:
                raise ProductError(
                    "duplicate_sku",
                    "SKU already exists",
                    [{"field": "sku", "message": "SKU already exists"}],
                )
            if len(self._products) >= self._capacity:
                raise ProductError("store_full", "Product store is full")
            product_id = self._next_id
            self._next_id += 1
            now = _timestamp()
            product = {"id": product_id, **fields, "created_at": now, "updated_at": now}
            self._products[product_id] = product
            self._sku_ids[fields["sku"]] = product_id
            return _copy_product(product)

    def get(self, product_id: int) -> dict[str, Any] | None:
        if not _valid_product_id(product_id):
            return None
        with self._lock:
            product = self._products.get(product_id)
            return _copy_product(product) if product is not None else None

    def list(
        self,
        category: str | None = None,
        tag: str | None = None,
        active: bool | None = None,
        in_stock: bool | None = None,
        min_price_cents: int | None = None,
        max_price_cents: int | None = None,
        q: str | None = None,
        sort: str = "id",
        limit: int = DEFAULT_LIMIT,
        offset: int = DEFAULT_OFFSET,
    ) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            matched = [
                product
                for product in self._products.values()
                if (category is None or product["category"] == category)
                and (tag is None or tag in product["tags"])
                and (active is None or product["active"] is active)
                and (in_stock is None or (product["stock"] > 0) is in_stock)
                and (
                    min_price_cents is None or product["price_cents"] >= min_price_cents
                )
                and (
                    max_price_cents is None or product["price_cents"] <= max_price_cents
                )
                and (
                    q is None
                    or q.casefold() in product["name"].casefold()
                    or q.casefold() in product["sku"].casefold()
                    or any(
                        q.casefold() in value.casefold() for value in product["tags"]
                    )
                )
            ]
            total = len(matched)
            descending = sort.startswith("-")
            key_name = sort[1:] if descending else sort
            if key_name not in {"id", "price_cents", "name", "created_at"}:
                raise ValueError("unsupported product sort")
            matched.sort(key=lambda product: product["id"])
            matched.sort(key=lambda product: product[key_name], reverse=descending)
            page = matched[offset : offset + limit] if offset < total else []
            return [_copy_product(product) for product in page], total

    def patch(self, product_id: int, **changes: Any) -> dict[str, Any] | None:
        fields = validate_patch(changes)
        if not _valid_product_id(product_id):
            return None
        with self._lock:
            current = self._products.get(product_id)
            if current is None:
                return None
            updated = {**current, **fields}
            now = _timestamp()
            if now == current["updated_at"]:
                now = (
                    (
                        datetime.fromisoformat(now.replace("Z", "+00:00"))
                        + timedelta(microseconds=1)
                    )
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            updated["updated_at"] = now
            self._products[product_id] = updated
            return _copy_product(updated)

    def delete(self, product_id: int) -> bool:
        if not _valid_product_id(product_id):
            return False
        with self._lock:
            product = self._products.pop(product_id, None)
            if product is None:
                return False
            self._sku_ids.pop(product["sku"], None)
            return True

    def adjust_stock(self, product_id: int, delta: int) -> dict[str, Any] | None:
        delta = validate_adjust_stock({"delta": delta})["delta"]
        if not _valid_product_id(product_id):
            return None
        with self._lock:
            current = self._products.get(product_id)
            if current is None:
                return None
            adjusted = current["stock"] + delta
            if adjusted < 0:
                raise ProductError("insufficient_stock", "Insufficient stock")
            if adjusted > MAX_STOCK:
                raise _validation_error(
                    [{"field": "stock", "message": f"Must be at most {MAX_STOCK}"}]
                )
            updated = dict(current)
            updated["stock"] = adjusted
            updated["updated_at"] = _timestamp()
            self._products[product_id] = updated
            return _copy_product(updated)

    def categories(self) -> list[dict[str, Any]]:
        with self._lock:
            aggregates: dict[str, dict[str, Any]] = {}
            for product in self._products.values():
                row = aggregates.get(product["category"])
                if row is None:
                    row = {
                        "category": product["category"],
                        "products": 0,
                        "active_products": 0,
                        "in_stock": 0,
                        "min_price_cents": product["price_cents"],
                        "max_price_cents": product["price_cents"],
                    }
                    aggregates[product["category"]] = row
                row["products"] += 1
                row["active_products"] += int(product["active"])
                row["in_stock"] += int(product["stock"] > 0)
                row["min_price_cents"] = min(
                    row["min_price_cents"], product["price_cents"]
                )
                row["max_price_cents"] = max(
                    row["max_price_cents"], product["price_cents"]
                )
            return [aggregates[key] for key in sorted(aggregates)]
