"""In-memory product storage and HTTP-independent validation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import threading
from typing import Any

from agent_qa.conditional import check_expected_version
from agent_qa.errors import ApiError
from agent_qa.pagination import (
    decode_cursor,
    encode_cursor,
    filter_fingerprint,
    keyset_page,
)
from agent_qa.schemas import (
    DEFAULT_LIMIT,
    DEFAULT_OFFSET,
    MAX_LIMIT,
    MAX_PRICE_CENTS,
    MAX_PRODUCTS,
    MAX_PRODUCT_QUERY_LENGTH,
    MAX_STOCK,
    MIN_LIMIT,
    MIN_OFFSET,
    PRODUCT_SORTS,
    SCHEMAS,
)
from agent_qa.validation import validate

_MAX_QUERY_PAIRS = 1000
_MAX_QUERY_TEXT_LENGTH = 4096
_CATEGORY_RE = re.compile(SCHEMAS["CreateProduct"]["properties"]["category"]["pattern"])
_TAG_RE = re.compile(SCHEMAS["CreateProduct"]["properties"]["tags"]["items"]["pattern"])


def _validation_error(details: list[dict[str, str]]) -> ApiError:
    details.sort(key=lambda item: item["field"])
    return ApiError(400, "validation_error", "Request validation failed", details)


def _raise_schema_errors(schema_name: str, payload: Any) -> None:
    errors = validate(SCHEMAS[schema_name], payload)
    if errors:
        raise _validation_error(errors)


def validate_create(payload: Any) -> dict[str, Any]:
    """Validate product fields and fill optional defaults."""
    candidate = payload if isinstance(payload, dict) else {}
    _raise_schema_errors("CreateProduct", candidate)
    return {
        "sku": candidate["sku"],
        "name": candidate["name"],
        "category": candidate["category"],
        "price_cents": candidate["price_cents"],
        "stock": candidate.get("stock", 0),
        "tags": list(candidate.get("tags", [])),
        "active": candidate.get("active", True),
    }


def validate_patch(payload: Any) -> dict[str, Any]:
    """Validate mutable product fields, preserving the immutable SKU error."""
    if isinstance(payload, dict) and "sku" in payload:
        raise _validation_error([{"field": "sku", "message": "Cannot be changed"}])
    _raise_schema_errors("UpdateProduct", payload)
    result = dict(payload)
    if "tags" in result:
        result["tags"] = list(result["tags"])
    return result


def validate_adjust_stock(payload: Any) -> dict[str, int]:
    """Validate a nonzero bounded stock adjustment."""
    _raise_schema_errors("AdjustStock", payload)
    return {"delta": payload["delta"]}


def _invalid_query(details: list[dict[str, str]]) -> ApiError:
    details.sort(key=lambda item: item["field"])
    return ApiError(400, "invalid_query", "Invalid query parameters", details)


def validate_query(query: list[tuple[str, str]]) -> dict[str, Any]:
    """Validate product-list parameters and return parsed filter values."""
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
        "pagination",
        "cursor",
    }
    values: dict[str, str] = {}
    errors: list[dict[str, str]] = []
    pairs = iter(query) if isinstance(query, (list, tuple)) else iter(())
    for index, pair in enumerate(pairs):
        if index >= _MAX_QUERY_PAIRS:
            errors.append({"field": "query", "message": "Too many query parameters"})
            break
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            errors.append({"field": "query", "message": "Invalid query parameter"})
            continue
        name, value = pair
        if not isinstance(name, str) or not isinstance(value, str):
            errors.append({"field": str(name), "message": "Invalid query parameter"})
        elif len(name) > _MAX_QUERY_TEXT_LENGTH or (
            name != "cursor" and len(value) > _MAX_QUERY_TEXT_LENGTH
        ):
            errors.append(
                {
                    "field": name[:_MAX_QUERY_TEXT_LENGTH],
                    "message": "Query parameter is too long",
                }
            )
        elif name not in allowed:
            errors.append({"field": name, "message": "Unsupported query parameter"})
        elif name in values:
            errors.append({"field": name, "message": "Parameter may appear once"})
        else:
            values[name] = value

    if "category" in values and not _CATEGORY_RE.fullmatch(values["category"]):
        errors.append({"field": "category", "message": "Invalid category"})
    if "tag" in values and not _TAG_RE.fullmatch(values["tag"]):
        errors.append({"field": "tag", "message": "Invalid tag"})
    for name in ("active", "in_stock"):
        if name in values and values[name] not in {"true", "false"}:
            errors.append({"field": name, "message": "Must be true or false"})
    if "q" in values and not 1 <= len(values["q"]) <= MAX_PRODUCT_QUERY_LENGTH:
        errors.append(
            {
                "field": "q",
                "message": f"Must contain 1 to {MAX_PRODUCT_QUERY_LENGTH} characters",
            }
        )
    if "sort" in values and values["sort"] not in PRODUCT_SORTS:
        errors.append({"field": "sort", "message": "Unsupported sort order"})

    explicit_offset = "offset" in values
    pagination = values.get("pagination", "offset")
    if pagination not in {"offset", "cursor"}:
        errors.append({"field": "pagination", "message": "Must be offset or cursor"})
    if explicit_offset and (pagination == "cursor" or "cursor" in values):
        errors.append(
            {"field": "cursor", "message": "Cannot combine cursor and offset"}
        )
    if "cursor" in values and values.get("pagination") == "offset":
        errors.append(
            {"field": "cursor", "message": "Cursor requires cursor pagination"}
        )

    parsed_numbers: dict[str, int] = {}
    bounds = {
        "min_price_cents": (0, MAX_PRICE_CENTS),
        "max_price_cents": (0, MAX_PRICE_CENTS),
        "limit": (MIN_LIMIT, MAX_LIMIT),
        "offset": (MIN_OFFSET, (1 << 63) - 1),
    }
    for name, (minimum, maximum) in bounds.items():
        if name not in values:
            continue
        try:
            number = int(values[name])
            if number < minimum or (maximum is not None and number > maximum):
                raise ValueError
        except ValueError:
            range_text = (
                f"{minimum} and {maximum}"
                if maximum is not None
                else f"at least {minimum}"
            )
            errors.append(
                {"field": name, "message": f"Must be an integer between {range_text}"}
            )
        else:
            parsed_numbers[name] = number
    if (
        "min_price_cents" in parsed_numbers
        and "max_price_cents" in parsed_numbers
        and parsed_numbers["min_price_cents"] > parsed_numbers["max_price_cents"]
    ):
        errors.append(
            {"field": "min_price_cents", "message": "Must not exceed max_price_cents"}
        )
    if errors:
        raise _invalid_query(errors)

    result: dict[str, Any] = {
        "limit": parsed_numbers.get("limit", DEFAULT_LIMIT),
        "sort": values.get("sort", "id"),
    }
    if pagination == "cursor" or "cursor" in values:
        result["pagination"] = "cursor"
        if "cursor" in values:
            result["cursor"] = values["cursor"]
    else:
        result["pagination"] = "offset"
        result["offset"] = parsed_numbers.get("offset", DEFAULT_OFFSET)
    for name in ("category", "tag", "q"):
        if name in values:
            result[name] = values[name]
    for name in ("active", "in_stock"):
        if name in values:
            result[name] = values[name] == "true"
    result.update(parsed_numbers)
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _next_timestamp(previous: str) -> str:
    now = datetime.now(timezone.utc)
    prior = datetime.fromisoformat(previous.replace("Z", "+00:00"))
    if now <= prior:
        now = prior + timedelta(microseconds=1)
    return now.isoformat().replace("+00:00", "Z")


def _copy_product(
    product: dict[str, Any], include_version: bool = False
) -> dict[str, Any]:
    copied = {**product, "tags": list(product["tags"])}
    if not include_version:
        copied.pop("version", None)
    return copied


def _invalid_store_query(field: str) -> ApiError:
    return _invalid_query([{"field": field, "message": "Invalid query value"}])


def _validate_store_query(query: dict[str, Any]) -> None:
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
        "pagination",
        "cursor",
    }
    for field in query.keys() - allowed:
        raise _invalid_store_query(str(field))
    for field in ("category", "tag"):
        if field in query and (
            not isinstance(query[field], str)
            or not (
                _CATEGORY_RE.fullmatch(query[field])
                if field == "category"
                else _TAG_RE.fullmatch(query[field])
            )
        ):
            raise _invalid_store_query(field)
    for field in ("active", "in_stock"):
        if field in query and not isinstance(query[field], bool):
            raise _invalid_store_query(field)
    for field in ("min_price_cents", "max_price_cents"):
        if field in query:
            value = query[field]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= MAX_PRICE_CENTS
            ):
                raise _invalid_store_query(field)
    if (
        "min_price_cents" in query
        and "max_price_cents" in query
        and query["min_price_cents"] > query["max_price_cents"]
    ):
        raise _invalid_store_query("min_price_cents")
    if "q" in query and (
        not isinstance(query["q"], str)
        or not 1 <= len(query["q"]) <= MAX_PRODUCT_QUERY_LENGTH
    ):
        raise _invalid_store_query("q")
    if "sort" in query and query["sort"] not in PRODUCT_SORTS:
        raise _invalid_store_query("sort")
    if "limit" in query:
        value = query["limit"]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not MIN_LIMIT <= value <= MAX_LIMIT
        ):
            raise _invalid_store_query("limit")
    if "offset" in query:
        value = query["offset"]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not MIN_OFFSET <= value <= (1 << 63) - 1
        ):
            raise _invalid_store_query("offset")
    if "pagination" in query and query["pagination"] not in {"offset", "cursor"}:
        raise _invalid_store_query("pagination")
    if (
        "cursor" in query
        and (
            not isinstance(query["cursor"], str)
            or query.get("pagination") == "offset"
            or "offset" in query
        )
    ) or (query.get("pagination") == "cursor" and "offset" in query):
        raise _invalid_store_query("cursor")


class ProductStore:
    """Thread-safe, process-local product storage with never-reused IDs."""

    def __init__(self, capacity: int = MAX_PRODUCTS) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise ValueError(f"capacity must be between 1 and {MAX_PRODUCTS}")
        if not 1 <= capacity <= MAX_PRODUCTS:
            raise ValueError(f"capacity must be between 1 and {MAX_PRODUCTS}")
        self._capacity = capacity
        self._products: dict[int, dict[str, Any]] = {}
        self._next_id = 1
        self._lock = threading.RLock()

    def create(self, *, include_version: bool = False, **fields: Any) -> dict[str, Any]:
        valid = validate_create(fields)
        with self._lock:
            if any(
                product["sku"] == valid["sku"] for product in self._products.values()
            ):
                raise ApiError(
                    409,
                    "duplicate_sku",
                    "Product SKU already exists",
                    [{"field": "sku", "message": "SKU already exists"}],
                )
            if len(self._products) >= self._capacity:
                raise ApiError(409, "store_full", "Product store is full")
            product_id = self._next_id
            self._next_id += 1
            timestamp = _now()
            product = {
                "id": product_id,
                **valid,
                "created_at": timestamp,
                "updated_at": timestamp,
                "version": 1,
            }
            self._products[product_id] = product
            return _copy_product(product, include_version)

    def get(
        self, product_id: int, *, include_version: bool = False
    ) -> dict[str, Any] | None:
        if (
            isinstance(product_id, bool)
            or not isinstance(product_id, int)
            or product_id <= 0
        ):
            return None
        with self._lock:
            product = self._products.get(product_id)
            return (
                _copy_product(product, include_version) if product is not None else None
            )

    def update(
        self,
        product_id: int,
        changes: dict[str, Any],
        *,
        expected_version: int | tuple[str, ...] | str | None = None,
        include_version: bool = False,
    ) -> dict[str, Any] | None:
        fields = validate_patch(changes)
        if (
            isinstance(product_id, bool)
            or not isinstance(product_id, int)
            or product_id <= 0
        ):
            return None
        with self._lock:
            current = self._products.get(product_id)
            if current is None:
                return None
            check_expected_version(
                expected_version, "product", product_id, current["version"]
            )
            updated_at = _next_timestamp(current["updated_at"])
            updated = {
                **current,
                **fields,
                "updated_at": updated_at,
                "version": current["version"] + 1,
            }
            self._products[product_id] = updated
            return _copy_product(updated, include_version)

    def delete(
        self,
        product_id: int,
        *,
        expected_version: int | tuple[str, ...] | str | None = None,
    ) -> bool:
        if (
            isinstance(product_id, bool)
            or not isinstance(product_id, int)
            or product_id <= 0
        ):
            return False
        with self._lock:
            current = self._products.get(product_id)
            if current is None:
                return False
            check_expected_version(
                expected_version, "product", product_id, current["version"]
            )
            del self._products[product_id]
            return True

    def _change_stock_locked(
        self, product_id: int, delta: int
    ) -> dict[str, Any] | None:
        current = self._products.get(product_id)
        if current is None:
            return None
        stock = current["stock"] + delta
        if stock < 0:
            raise ApiError(409, "insufficient_stock", "Insufficient stock")
        if stock > MAX_STOCK:
            raise _validation_error(
                [{"field": "stock", "message": f"Must be at most {MAX_STOCK}"}]
            )
        updated = {
            **current,
            "stock": stock,
            "updated_at": _next_timestamp(current["updated_at"]),
            "version": current["version"] + 1,
        }
        self._products[product_id] = updated
        return updated

    def adjust_stock(
        self,
        product_id: int,
        delta: int,
        *,
        expected_version: int | tuple[str, ...] | str | None = None,
        include_version: bool = False,
    ) -> dict[str, Any] | None:
        valid = validate_adjust_stock({"delta": delta})
        if (
            isinstance(product_id, bool)
            or not isinstance(product_id, int)
            or product_id <= 0
        ):
            return None
        with self._lock:
            current = self._products.get(product_id)
            if current is None:
                return None
            check_expected_version(
                expected_version, "product", product_id, current["version"]
            )
            updated = self._change_stock_locked(product_id, valid["delta"])
            return (
                _copy_product(updated, include_version) if updated is not None else None
            )

    def reserve(self, lines: list[dict[str, Any]]) -> None:
        """Atomically decrement stock for validated order lines.

        Each line contains ``product_id``, ``quantity`` and ``index`` (zero based).
        The caller holds the product lock for its surrounding transaction.
        """
        with self._lock:
            shortages = []
            for line in lines:
                product = self._products.get(line["product_id"])
                if product is None:
                    continue
                if product["stock"] < line["quantity"]:
                    shortages.append(
                        {
                            "field": f"items[{line['index']}].quantity",
                            "message": f"Only {product['stock']} in stock",
                        }
                    )
            if shortages:
                raise ApiError(
                    409,
                    "insufficient_stock",
                    "Insufficient stock",
                    shortages,
                )
            for line in lines:
                self._change_stock_locked(line["product_id"], -line["quantity"])

    def release(self, lines: list[dict[str, Any]]) -> None:
        """Atomically return stock for validated order lines.

        Deleted products are skipped because their inventory no longer exists.
        """
        with self._lock:
            products = []
            for line in lines:
                product = self._products.get(line["product_id"])
                if product is not None:
                    if product["stock"] + line["quantity"] > MAX_STOCK:
                        raise _validation_error(
                            [
                                {
                                    "field": "stock",
                                    "message": f"Must be at most {MAX_STOCK}",
                                }
                            ]
                        )
                    products.append(line)
            for line in products:
                self._change_stock_locked(line["product_id"], line["quantity"])

    def _snapshot_stock_locked(
        self, product_ids: set[int]
    ) -> dict[int, dict[str, Any]]:
        return {
            product_id: dict(self._products[product_id])
            for product_id in product_ids
            if product_id in self._products
        }

    def _restore_stock_locked(self, snapshot: dict[int, dict[str, Any]]) -> None:
        for product_id, product in snapshot.items():
            self._products[product_id] = product

    def list(
        self, **query: Any
    ) -> (
        tuple[list[dict[str, Any]], int] | tuple[list[dict[str, Any]], int, str | None]
    ):
        _validate_store_query(query)
        limit = query.get("limit", DEFAULT_LIMIT)
        offset = query.get("offset", DEFAULT_OFFSET)
        sort = query.get("sort", "id")
        cursor_mode = query.get("pagination", "offset") == "cursor" or "cursor" in query
        with self._lock:
            matched = []
            for product_id in sorted(self._products):
                product = self._products[product_id]
                if (
                    query.get("category") is not None
                    and product["category"] != query["category"]
                ):
                    continue
                if query.get("tag") is not None and query["tag"] not in product["tags"]:
                    continue
                if (
                    query.get("active") is not None
                    and product["active"] != query["active"]
                ):
                    continue
                if query.get("in_stock") is True and product["stock"] <= 0:
                    continue
                if query.get("in_stock") is False and product["stock"] > 0:
                    continue
                if product["price_cents"] < query.get("min_price_cents", 0):
                    continue
                if product["price_cents"] > query.get(
                    "max_price_cents", MAX_PRICE_CENTS
                ):
                    continue
                search = query.get("q")
                if search is not None:
                    needle = search.casefold()
                    searchable = [product["name"], product["sku"], *product["tags"]]
                    if not any(needle in value.casefold() for value in searchable):
                        continue
                matched.append(product)
            total = len(matched)
            if cursor_mode:
                filters = {
                    name: query[name]
                    for name in (
                        "category",
                        "tag",
                        "active",
                        "in_stock",
                        "min_price_cents",
                        "max_price_cents",
                        "q",
                    )
                    if name in query
                }
                fingerprint = filter_fingerprint(filters)
                cursor = query.get("cursor")
                cursor_key = (
                    decode_cursor(cursor, sort, fingerprint)
                    if cursor is not None
                    else None
                )
                descending = sort.startswith("-")
                field = sort.lstrip("-")
                page, has_more = keyset_page(
                    matched, (field, descending), cursor_key, limit
                )
                if has_more and page:
                    last = page[-1]
                    next_cursor = encode_cursor(
                        sort, fingerprint, [last[field], last["id"]]
                    )
                else:
                    next_cursor = None
                return [_copy_product(product) for product in page], total, next_cursor
            if sort in {"id", "-id"}:
                matched.sort(key=lambda product: product["id"], reverse=sort == "-id")
            elif sort in {"price_cents", "-price_cents"}:
                matched.sort(key=lambda product: product["id"])
                matched.sort(
                    key=lambda product: product["price_cents"],
                    reverse=sort.startswith("-"),
                )
            elif sort in {"name", "-name"}:
                matched.sort(key=lambda product: product["id"])
                matched.sort(
                    key=lambda product: product["name"], reverse=sort.startswith("-")
                )
            elif sort == "created_at":
                matched.sort(key=lambda product: product["id"])
                matched.sort(key=lambda product: product["created_at"])
            page = matched[offset : offset + limit] if offset < total else []
            return [_copy_product(product) for product in page], total

    def categories(self) -> list[dict[str, Any]]:
        with self._lock:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for product in self._products.values():
                grouped.setdefault(product["category"], []).append(product)
            return [
                {
                    "category": category,
                    "products": len(products),
                    "active_products": sum(product["active"] for product in products),
                    "in_stock": sum(product["stock"] > 0 for product in products),
                    "min_price_cents": min(
                        product["price_cents"] for product in products
                    ),
                    "max_price_cents": max(
                        product["price_cents"] for product in products
                    ),
                }
                for category, products in sorted(grouped.items())
            ]
