"""Pure route handlers for the agent QA HTTP service."""

import hashlib
import json
import os
import platform
from collections.abc import Callable
from urllib.parse import quote, urlencode

from agent_qa import auth
from agent_qa import csvio
from agent_qa.audit import AUDIT_LOG
from agent_qa.config import FIXTURE_PATH, GIT_SHA
from agent_qa.conditional import etag_for, parse_etag_list, weak_match
from agent_qa.context import get_context
from agent_qa.errors import ApiError
from agent_qa.fulfillment import FulfillmentService
from agent_qa.metrics import REGISTRY
from agent_qa.openapi import build_openapi
from agent_qa.pagination import MAX_CURSOR_LENGTH
from agent_qa.schemas import (
    DEFAULT_LIMIT,
    DEFAULT_OFFSET,
    MAX_LIMIT,
    MIN_LIMIT,
    MIN_OFFSET,
    MAX_PRODUCT_QUERY_LENGTH,
    PRODUCT_SORTS,
    SCHEMAS,
)
from agent_qa.validation import validate, validate_query_params
from agent_qa.orders import (
    OrderStore,
    validate_create,
    validate_patch,
    validate_query,
)
from agent_qa.products import (
    ProductStore,
    validate_adjust_stock,
    validate_create as validate_product_create,
    validate_patch as validate_product_patch,
    validate_query as validate_product_query,
)


ORDER_STORE = OrderStore()
PRODUCT_STORE = ProductStore()


def _header(headers: dict[str, str] | None, name: str) -> str | None:
    if not headers:
        return None
    return next(
        (value for key, value in headers.items() if key.lower() == name.lower()), None
    )


def _list_etag(body: object) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f'W/"{digest}"'


def _cursor_link(path: str, filters: dict[str, object], cursor: str) -> str:
    parameters = [
        (key, str(value).lower() if isinstance(value, bool) else value)
        for key, value in filters.items()
        if key not in {"offset", "pagination", "cursor"} and value is not None
    ]
    parameters.append(("pagination", "cursor"))
    parameters.append(("cursor", cursor))
    return f'<{path}?{urlencode(parameters, quote_via=quote)}>; rel="next"'


def _if_none_match(headers: dict[str, str] | None, current: str) -> bool:
    raw = _header(headers, "If-None-Match")
    return raw is not None and weak_match(parse_etag_list(raw), current)


def _expected_version(headers: dict[str, str] | None) -> tuple[str, ...] | str | None:
    raw = _header(headers, "If-Match")
    if raw is None:
        if os.environ.get("AGENT_QA_REQUIRE_IF_MATCH", "").lower() == "true":
            raise ApiError(428, "precondition_required", "If-Match is required")
        return None
    return parse_etag_list(raw)


def ready(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return the service readiness document."""
    return 200, {"status": "ready", "git_sha": GIT_SHA}, {}


def about(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return the service name and build SHA."""
    return (
        200,
        {"service": "agent-qa", "git_sha": GIT_SHA, "environment": "qa"},
        {"X-Service": "agent-qa"},
    )


def ping(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return a simple liveness response."""
    return 200, {"pong": True}, {}


def health(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return the service health response."""
    return 200, {"status": "ok"}, {}


def status(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Report service status and whether the fixture can be read as JSON."""
    try:
        with FIXTURE_PATH.open(encoding="utf-8") as fixture_file:
            json.load(fixture_file)
    except (OSError, UnicodeError, json.JSONDecodeError):
        fixture_ok = False
    else:
        fixture_ok = True
    return (
        200,
        {
            "status": "ok" if fixture_ok else "degraded",
            "service": "agent-qa",
            "checks": {"fixture": fixture_ok},
        },
        {},
    )


def metrics(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return a Prometheus snapshot of service metrics."""
    order_count = ORDER_STORE.list(limit=1)[1]
    product_count = PRODUCT_STORE.list(limit=1)[1]
    audit_entries, audit_dropped = AUDIT_LOG.metrics_snapshot()
    return (
        200,
        REGISTRY.render(
            order_count,
            GIT_SHA,
            products=product_count,
            audit_entries=audit_entries,
            audit_dropped=audit_dropped,
        ),
        {"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
    )


def whoami(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
    *,
    identity: dict[str, str] | None = None,
) -> tuple[int, object, dict[str, str]]:
    """Return the authenticated key's public identity fields."""
    if identity is None:
        raise ApiError(401, "unauthorized", "A valid API key is required")
    return 200, {name: identity[name] for name in ("key_id", "role", "label")}, {}


def _key_payload(payload: object, allowed: set[str]) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ApiError(400, "validation_error", "Invalid request body")
    return payload


def _key_id(path_params: dict[str, str] | None) -> str:
    key_id = (path_params or {}).get("key_id", "")
    if not key_id or len(key_id) > 32:
        raise ApiError(404, "key_not_found", "Key not found")
    return key_id


def create_api_key(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    body = _key_payload(payload, {"role", "label"})
    role, label = body.get("role"), body.get("label")
    if not isinstance(role, str) or role not in {"read", "write", "admin"}:
        raise ApiError(400, "invalid_role", "Role must be read, write, or admin")
    if not isinstance(label, str) or not label.strip() or len(label) > 40:
        raise ApiError(
            400, "validation_error", "Label must be 1 to 40 non-blank characters"
        )
    return 201, auth.KEY_STORE.create(role, label), {}


def list_api_keys(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    return 200, auth.KEY_STORE.list_keys(), {}


def rotate_api_key(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    body = _key_payload(payload, {"grace_seconds"})
    grace = body.get("grace_seconds", 0)
    if type(grace) is not int or not 0 <= grace <= 300:
        raise ApiError(
            400, "validation_error", "grace_seconds must be an integer from 0 to 300"
        )
    return 200, auth.KEY_STORE.rotate(_key_id(path_params), grace), {}


def revoke_api_key(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    auth.KEY_STORE.revoke(_key_id(path_params))
    return 204, None, {"Content-Length": "0"}


def fixture(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Read the synthetic fixture and optionally project requested fields."""
    field_values = [value for name, value in query if name == "fields"]
    invalid_param = next((name for name, _ in query if name != "fields"), None)
    if invalid_param is not None:
        message = f"Unsupported query parameter: {invalid_param}"
        raise ApiError(
            400,
            "invalid_query",
            message,
            [{"param": invalid_param, "message": message}],
        )
    if len(field_values) > 1:
        message = "The fields parameter may appear once"
        raise ApiError(
            400, "invalid_query", message, [{"param": "fields", "message": message}]
        )
    with FIXTURE_PATH.open(encoding="utf-8") as fixture_file:
        data = json.load(fixture_file)
    if field_values:
        requested_fields = field_values[0].split(",")
        if any(not field for field in requested_fields):
            message = "Fields must not be empty"
            raise ApiError(
                400,
                "invalid_query",
                message,
                [{"param": "fields", "message": message}],
            )
        unknown_fields = [field for field in requested_fields if field not in data]
        if unknown_fields:
            message = f"Unknown field: {unknown_fields[0]}"
            raise ApiError(
                400,
                "invalid_query",
                message,
                [{"param": "fields", "message": message}],
            )
        data = {field: data[field] for field in dict.fromkeys(requested_fields)}
    return 200, data, {}


def version(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return service and Python version details."""
    return (
        200,
        {
            "service": "agent-qa",
            "git_sha": GIT_SHA,
            "python_version": platform.python_version(),
        },
        {},
    )


def openapi(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return the OpenAPI document generated from the route table."""
    return 200, build_openapi(ROUTES, GIT_SHA), {}


def list_schemas(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return the registered schema names in deterministic order."""
    return 200, {"items": sorted(SCHEMAS)}, {}


def get_schema(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return a named schema or the standard schema-not-found error."""
    name = (path_params or {}).get("name", "")
    if name not in SCHEMAS:
        raise ApiError(404, "schema_not_found", "Schema not found")
    return 200, SCHEMAS[name], {}


def validate_named_schema(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Validate a JSON value against a named schema."""
    name = (path_params or {}).get("name", "")
    if name not in SCHEMAS:
        raise ApiError(404, "schema_not_found", "Schema not found")
    errors = validate(SCHEMAS[name], payload)
    return 200, {"valid": not errors, "errors": errors}, {}


def create_order(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Validate and create an order."""
    values = validate_create(payload)
    order = FulfillmentService(ORDER_STORE, PRODUCT_STORE).create(
        **values, include_version=True
    )
    version = order.pop("version")
    return (
        201,
        order,
        {
            "Location": f"/orders/{order['id']}",
            "ETag": etag_for("order", order["id"], version),
        },
    )


def create_orders_bulk(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Create orders in sequence and return per-item results."""
    values = payload if isinstance(payload, dict) else {}
    status, body = FulfillmentService(ORDER_STORE, PRODUCT_STORE).create_bulk(
        values.get("items"), values.get("atomic", False)
    )
    context = get_context()
    if context is not None and isinstance(body, dict):
        context.resource = "orders"
        context.changes = {"summary": dict(body.get("summary", {}))}
    return status, body, {}


def list_orders(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
    request_headers: dict[str, str] | None = None,
) -> tuple[int, object, dict[str, str]]:
    """Return filtered and paginated orders."""
    filters = validate_query(query)
    result = ORDER_STORE.list(**filters)
    if filters["pagination"] == "cursor":
        items, total, next_cursor = result
        body = {
            "items": items,
            "total": total,
            "limit": filters["limit"],
            "next_cursor": next_cursor,
        }
    else:
        items, total = result
        body = {
            "items": items,
            "total": total,
            "limit": filters["limit"],
            "offset": filters["offset"],
        }
    etag = _list_etag(body)
    headers = {"ETag": etag}
    if filters["pagination"] == "cursor" and next_cursor is not None:
        headers["Link"] = _cursor_link("/orders", filters, next_cursor)
    if _if_none_match(request_headers, etag):
        return 304, None, headers
    return 200, body, headers


def _order_id(path_params: dict[str, str] | None) -> int | None:
    raw_id = (path_params or {}).get("id", "")
    if len(raw_id) > 20 or not raw_id.isascii() or not raw_id.isdigit():
        return None
    try:
        order_id = int(raw_id)
    except ValueError:
        return None
    return order_id if order_id > 0 else None


def get_order(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
    request_headers: dict[str, str] | None = None,
) -> tuple[int, object, dict[str, str]]:
    """Return one order or the standard missing-order error."""
    order_id = _order_id(path_params)
    order = (
        ORDER_STORE.get(order_id, include_version=True)
        if order_id is not None
        else None
    )
    if order is None:
        raise ApiError(404, "order_not_found", "Order not found")
    version = order.pop("version")
    etag = etag_for("order", order_id, version)
    if _if_none_match(request_headers, etag):
        return 304, None, {"ETag": etag}
    return 200, order, {"ETag": etag}


def patch_order(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
    request_headers: dict[str, str] | None = None,
) -> tuple[int, object, dict[str, str]]:
    """Validate and update an order."""
    changes = validate_patch(payload)
    order_id = _order_id(path_params)
    if order_id is None:
        raise ApiError(404, "order_not_found", "Order not found")
    if ORDER_STORE.get(order_id) is None:
        raise ApiError(404, "order_not_found", "Order not found")
    expected_version = _expected_version(request_headers)
    order = FulfillmentService(ORDER_STORE, PRODUCT_STORE).update(
        order_id, changes, expected_version=expected_version, include_version=True
    )
    if order is None:
        raise ApiError(404, "order_not_found", "Order not found")
    version = order.pop("version")
    return 200, order, {"ETag": etag_for("order", order_id, version)}


def delete_order(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
    request_headers: dict[str, str] | None = None,
) -> tuple[int, object, dict[str, str]]:
    """Delete an order and return an empty response body."""
    order_id = _order_id(path_params)
    if order_id is None or ORDER_STORE.get(order_id) is None:
        raise ApiError(404, "order_not_found", "Order not found")
    expected_version = _expected_version(request_headers)
    if not FulfillmentService(ORDER_STORE, PRODUCT_STORE).delete(
        order_id, expected_version=expected_version
    ):
        raise ApiError(404, "order_not_found", "Order not found")
    return 204, None, {"Content-Length": "0"}


def create_product(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Validate and create a product."""
    product = PRODUCT_STORE.create(
        **validate_product_create(payload), include_version=True
    )
    version = product.pop("version")
    return (
        201,
        product,
        {
            "Location": f"/products/{product['id']}",
            "ETag": etag_for("product", product["id"], version),
        },
    )


def create_products_bulk(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Create products in sequence and return per-item results."""
    values = payload if isinstance(payload, dict) else {}
    status, body = PRODUCT_STORE.create_bulk(
        values.get("items"), values.get("atomic", False)
    )
    context = get_context()
    if context is not None and isinstance(body, dict):
        context.resource = "products"
        context.changes = {"summary": dict(body.get("summary", {}))}
    return status, body, {}


def list_products(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
    request_headers: dict[str, str] | None = None,
) -> tuple[int, object, dict[str, str]]:
    """Return filtered and paginated products."""
    filters = validate_product_query(query)
    result = PRODUCT_STORE.list(**filters)
    if filters["pagination"] == "cursor":
        items, total, next_cursor = result
        body = {
            "items": items,
            "total": total,
            "limit": filters["limit"],
            "next_cursor": next_cursor,
        }
    else:
        items, total = result
        body = {
            "items": items,
            "total": total,
            "limit": filters["limit"],
            "offset": filters["offset"],
        }
    etag = _list_etag(body)
    headers = {"ETag": etag}
    if filters["pagination"] == "cursor" and next_cursor is not None:
        headers["Link"] = _cursor_link("/products", filters, next_cursor)
    if _if_none_match(request_headers, etag):
        return 304, None, headers
    return 200, body, headers


_EXPORT_PRODUCT_COLUMNS = (
    "id",
    "sku",
    "name",
    "category",
    "price_cents",
    "stock",
    "tags",
    "active",
    "created_at",
    "updated_at",
)
_EXPORT_ORDER_COLUMNS = (
    "id",
    "customer_id",
    "total_cents",
    "status",
    "items_count",
    "created_at",
)
_EXPORT_PRODUCT_QUERY = [
    {
        "name": "category",
        "in": "query",
        "schema": SCHEMAS["CreateProduct"]["properties"]["category"],
    },
    {"name": "active", "in": "query", "schema": {"type": "boolean"}},
    {"name": "in_stock", "in": "query", "schema": {"type": "boolean"}},
    {
        "name": "q",
        "in": "query",
        "schema": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_PRODUCT_QUERY_LENGTH,
        },
    },
]
_EXPORT_ORDER_QUERY = [
    {
        "name": "status",
        "in": "query",
        "schema": SCHEMAS["UpdateOrder"]["properties"]["status"],
    },
    {
        "name": "customer_id",
        "in": "query",
        "schema": SCHEMAS["CreateOrder"]["properties"]["customer_id"],
    },
]
_CSV_IMPORT_QUERY = [
    {
        "name": "mode",
        "in": "query",
        "schema": {
            "type": "string",
            "enum": ["apply", "validate"],
            "default": "apply",
        },
    },
    {
        "name": "on_error",
        "in": "query",
        "schema": {
            "type": "string",
            "enum": ["abort", "skip"],
            "default": "abort",
        },
    },
]

_CSV_IMPORT_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["mode", "rows", "created", "failed", "applied", "errors"],
    "properties": {
        "mode": {"type": "string", "enum": ["apply", "validate"]},
        "rows": {"type": "integer", "minimum": 0},
        "created": {"type": "integer", "minimum": 0},
        "failed": {"type": "integer", "minimum": 0},
        "applied": {"type": "boolean"},
        "errors": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["line", "field", "message"],
                "properties": {
                    "line": {"type": "integer", "minimum": 2},
                    "field": {"type": "string"},
                    "message": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


def _csv_download(
    filename: str,
    columns: tuple[str, ...],
    rows: list[dict],
    text_columns: set[str],
) -> tuple[int, str, dict[str, str]]:
    text = csvio.render_csv(columns, rows, text_columns=text_columns)
    return (
        200,
        text,
        {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


def export_products_csv(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, str, dict[str, str]]:
    parsed = validate_product_query(query)
    unsupported = sorted(
        {name for name, _ in query} - {"category", "active", "in_stock", "q"}
    )
    if unsupported:
        raise ApiError(
            400,
            "invalid_query",
            "Invalid query parameters",
            [
                {"field": name, "message": "Unsupported query parameter"}
                for name in unsupported
            ],
        )
    filters = {
        name: parsed[name]
        for name in ("category", "active", "in_stock", "q")
        if name in parsed
    }
    rows = PRODUCT_STORE.export_csv(filters)
    return _csv_download(
        "products.csv",
        _EXPORT_PRODUCT_COLUMNS,
        rows,
        {"sku", "name", "category", "tags", "created_at", "updated_at"},
    )


def export_orders_csv(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, str, dict[str, str]]:
    parsed = validate_query(query)
    unsupported = sorted({name for name, _ in query} - {"status", "customer_id"})
    if unsupported:
        raise ApiError(
            400,
            "invalid_query",
            "Invalid query parameters",
            [
                {"field": name, "message": "Unsupported query parameter"}
                for name in unsupported
            ],
        )
    filters = {
        name: parsed[name] for name in ("status", "customer_id") if name in parsed
    }
    rows = ORDER_STORE.export_csv(filters)
    return _csv_download(
        "orders.csv",
        _EXPORT_ORDER_COLUMNS,
        rows,
        {"customer_id", "status", "created_at"},
    )


def _import_csv(
    importer: Callable,
    store: object,
    payload: object,
    query: list[tuple[str, str]],
) -> tuple[int, object, dict[str, str]]:
    if not isinstance(payload, str):
        raise ApiError(
            400,
            "invalid_csv",
            "CSV body is required",
            [{"field": "body", "message": "CSV body is required"}],
        )
    options = validate_query_params(_CSV_IMPORT_QUERY, query)
    mode = options.get("mode", "apply")
    on_error = options.get("on_error", "abort")
    try:
        status_code, report = importer(store, payload, mode=mode, on_error=on_error)
    except csvio.CsvStructureError as error:
        raise ApiError(
            400,
            "invalid_csv",
            "Invalid CSV structure",
            [{"field": error.field, "message": error.message}],
        ) from error
    context = get_context()
    if context is not None and isinstance(report, dict):
        context.changes = {
            "summary": {
                "created": report.get("created", 0),
                "failed": report.get("failed", 0),
                "rows": report.get("rows", 0),
            }
        }
    return status_code, report, {}


def import_products_csv(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    return _import_csv(csvio.import_products_csv, PRODUCT_STORE, payload, query)


def import_orders_csv(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    return _import_csv(csvio.import_orders_csv, ORDER_STORE, payload, query)


def _product_id(path_params: dict[str, str] | None) -> int | None:
    raw_id = (path_params or {}).get("id", "")
    if len(raw_id) > 20 or not raw_id.isascii() or not raw_id.isdigit():
        return None
    try:
        product_id = int(raw_id)
    except ValueError:
        return None
    return product_id if product_id > 0 else None


def get_product(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
    request_headers: dict[str, str] | None = None,
) -> tuple[int, object, dict[str, str]]:
    """Return one product or the standard missing-product error."""
    product_id = _product_id(path_params)
    product = (
        PRODUCT_STORE.get(product_id, include_version=True)
        if product_id is not None
        else None
    )
    if product is None:
        raise ApiError(404, "product_not_found", "Product not found")
    version = product.pop("version")
    etag = etag_for("product", product_id, version)
    if _if_none_match(request_headers, etag):
        return 304, None, {"ETag": etag}
    return 200, product, {"ETag": etag}


def patch_product(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
    request_headers: dict[str, str] | None = None,
) -> tuple[int, object, dict[str, str]]:
    """Validate and update a product."""
    changes = validate_product_patch(payload)
    product_id = _product_id(path_params)
    if product_id is None:
        raise ApiError(404, "product_not_found", "Product not found")
    if PRODUCT_STORE.get(product_id) is None:
        raise ApiError(404, "product_not_found", "Product not found")
    expected_version = _expected_version(request_headers)
    product = PRODUCT_STORE.update(
        product_id, changes, expected_version=expected_version, include_version=True
    )
    if product is None:
        raise ApiError(404, "product_not_found", "Product not found")
    version = product.pop("version")
    return 200, product, {"ETag": etag_for("product", product_id, version)}


def delete_product(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
    request_headers: dict[str, str] | None = None,
) -> tuple[int, object, dict[str, str]]:
    """Delete a product and return an empty response body."""
    product_id = _product_id(path_params)
    if product_id is None or PRODUCT_STORE.get(product_id) is None:
        raise ApiError(404, "product_not_found", "Product not found")
    expected_version = _expected_version(request_headers)
    if not PRODUCT_STORE.delete(product_id, expected_version=expected_version):
        raise ApiError(404, "product_not_found", "Product not found")
    return 204, None, {"Content-Length": "0"}


def adjust_product_stock(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
    request_headers: dict[str, str] | None = None,
) -> tuple[int, object, dict[str, str]]:
    """Atomically adjust one product's stock."""
    values = validate_adjust_stock(payload)
    product_id = _product_id(path_params)
    if product_id is None:
        raise ApiError(404, "product_not_found", "Product not found")
    if PRODUCT_STORE.get(product_id) is None:
        raise ApiError(404, "product_not_found", "Product not found")
    expected_version = _expected_version(request_headers)
    product = PRODUCT_STORE.adjust_stock(
        product_id,
        values["delta"],
        expected_version=expected_version,
        include_version=True,
    )
    if product is None:
        raise ApiError(404, "product_not_found", "Product not found")
    version = product.pop("version")
    return 200, product, {"ETag": etag_for("product", product_id, version)}


def categories(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return current aggregates for categories represented by products."""
    items = PRODUCT_STORE.categories()
    return 200, {"items": items, "total": len(items)}, {}


def list_audit(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return bounded, filtered audit entries for administrators."""
    filters = validate_query_params(_AUDIT_QUERY_PARAMETERS, query)
    return 200, AUDIT_LOG.query(**filters), {}


def get_audit_entry(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return one retained audit entry by its sequence number."""
    raw_seq = (path_params or {}).get("seq", "")
    if len(raw_seq) > 20 or not raw_seq.isascii() or not raw_seq.isdigit():
        entry = None
    else:
        try:
            seq = int(raw_seq)
        except ValueError:
            seq = 0
        entry = AUDIT_LOG.get(seq) if seq > 0 else None
    if entry is None:
        raise ApiError(404, "audit_entry_not_found", "Audit entry not found")
    return 200, entry, {}


_ORDER_ID = {
    "name": "id",
    "in": "path",
    "required": True,
    "description": "Positive order identifier.",
    "schema": {"type": "integer", "minimum": 1},
}
_CUSTOMER_ID_SCHEMA = SCHEMAS["CreateOrder"]["properties"]["customer_id"]
_STATUS_SCHEMA = SCHEMAS["UpdateOrder"]["properties"]["status"]
_CREATE_ORDER_SCHEMA = SCHEMAS["CreateOrder"]
_UPDATE_ORDER_SCHEMA = SCHEMAS["UpdateOrder"]
_ORDER_RESPONSE_SCHEMA = SCHEMAS["Order"]
_ORDER_LIST_RESPONSE_SCHEMA = SCHEMAS["OrderList"]
_PRODUCT_ID = {
    "name": "id",
    "in": "path",
    "required": True,
    "description": "Positive product identifier.",
    "schema": {"type": "integer", "minimum": 1},
}
_IF_NONE_MATCH = {
    "name": "If-None-Match",
    "in": "header",
    "required": False,
    "description": "Return 304 when the current ETag weakly matches this value.",
    "schema": {"type": "string"},
}
_IF_MATCH = {
    "name": "If-Match",
    "in": "header",
    "required": False,
    "description": (
        "Apply the change only when a strong ETag matches; server configuration "
        "may require this header."
    ),
    "schema": {"type": "string"},
}
_CREATE_PRODUCT_SCHEMA = SCHEMAS["CreateProduct"]
_UPDATE_PRODUCT_SCHEMA = SCHEMAS["UpdateProduct"]
_PRODUCT_RESPONSE_SCHEMA = SCHEMAS["Product"]
_PRODUCT_LIST_RESPONSE_SCHEMA = SCHEMAS["ProductList"]
_ADJUST_STOCK_SCHEMA = SCHEMAS["AdjustStock"]
_CATEGORY_LIST_RESPONSE_SCHEMA = SCHEMAS["CategoryList"]
_AUDIT_QUERY_PARAMETERS = [
    {
        "name": "method",
        "in": "query",
        "schema": {"type": "string", "enum": ["POST", "PUT", "PATCH", "DELETE"]},
    },
    {
        "name": "resource",
        "in": "query",
        "schema": {"type": "string", "enum": ["orders", "products", "keys"]},
    },
    {
        "name": "resource_id",
        "in": "query",
        "schema": {"type": "string", "maxLength": 64},
    },
    {
        "name": "outcome",
        "in": "query",
        "schema": {
            "type": "string",
            "enum": ["success", "denied", "rejected", "error"],
        },
    },
    {
        "name": "actor",
        "in": "query",
        "schema": {"type": "string", "maxLength": 128},
    },
    {
        "name": "status",
        "in": "query",
        "schema": {"type": "integer", "minimum": 100, "maximum": 599},
    },
    {
        "name": "since_seq",
        "in": "query",
        "schema": {
            "type": "integer",
            "minimum": 0,
            "maximum": (1 << 63) - 1,
            "default": 0,
        },
    },
    {
        "name": "limit",
        "in": "query",
        "schema": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
    },
    {
        "name": "order",
        "in": "query",
        "schema": {
            "type": "string",
            "enum": ["asc", "desc"],
            "default": "desc",
        },
    },
]
_AUDIT_ENTRY_SCHEMA = {
    "type": "object",
    "required": [
        "seq",
        "ts",
        "actor",
        "role",
        "method",
        "route",
        "path",
        "resource",
        "resource_id",
        "status",
        "outcome",
        "request_id",
        "changes",
    ],
    "properties": {
        "seq": {"type": "integer", "minimum": 1},
        "ts": {"type": "string", "format": "date-time"},
        "actor": {"type": "string", "minLength": 1, "maxLength": 128},
        "role": {
            "type": "string",
            "enum": ["read", "write", "admin"],
            "nullable": True,
        },
        "method": {"type": "string", "enum": ["POST", "PUT", "PATCH", "DELETE"]},
        "route": {"type": "string"},
        "path": {"type": "string"},
        "resource": {
            "type": "string",
            "enum": ["orders", "products", "keys"],
            "nullable": True,
        },
        "resource_id": {
            "oneOf": [{"type": "integer"}, {"type": "string", "nullable": True}],
        },
        "status": {"type": "integer", "minimum": 100, "maximum": 599},
        "outcome": {
            "type": "string",
            "enum": ["success", "denied", "rejected", "error"],
        },
        "request_id": {"type": "string"},
        "changes": {"type": "object", "nullable": True},
        "replay": {"type": "boolean", "enum": [True]},
    },
    "additionalProperties": False,
}
_AUDIT_LIST_RESPONSE_SCHEMA = {
    "type": "object",
    "required": [
        "items",
        "total_matching",
        "limit",
        "capacity",
        "dropped",
        "last_seq",
    ],
    "properties": {
        "items": {"type": "array", "items": _AUDIT_ENTRY_SCHEMA},
        "total_matching": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        "capacity": {"type": "integer", "minimum": 10, "maximum": 5000},
        "dropped": {"type": "integer", "minimum": 0},
        "last_seq": {"type": "integer", "minimum": 0},
    },
    "additionalProperties": False,
}
_PRODUCT_QUERY_PARAMETERS = [
    {
        "name": "category",
        "in": "query",
        "required": False,
        "schema": _CREATE_PRODUCT_SCHEMA["properties"]["category"],
    },
    {
        "name": "tag",
        "in": "query",
        "required": False,
        "schema": _CREATE_PRODUCT_SCHEMA["properties"]["tags"]["items"],
    },
    {
        "name": "active",
        "in": "query",
        "required": False,
        "schema": {"type": "boolean"},
    },
    {
        "name": "in_stock",
        "in": "query",
        "required": False,
        "schema": {"type": "boolean"},
    },
    {
        "name": "min_price_cents",
        "in": "query",
        "required": False,
        "schema": _CREATE_PRODUCT_SCHEMA["properties"]["price_cents"],
    },
    {
        "name": "max_price_cents",
        "in": "query",
        "required": False,
        "schema": _CREATE_PRODUCT_SCHEMA["properties"]["price_cents"],
    },
    {
        "name": "q",
        "in": "query",
        "required": False,
        "schema": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_PRODUCT_QUERY_LENGTH,
        },
    },
    {
        "name": "sort",
        "in": "query",
        "required": False,
        "schema": {
            "type": "string",
            "enum": list(PRODUCT_SORTS),
            "default": "id",
        },
    },
    {
        "name": "limit",
        "in": "query",
        "required": False,
        "description": "Maximum number of products to return.",
        "schema": {
            "type": "integer",
            "minimum": MIN_LIMIT,
            "maximum": MAX_LIMIT,
            "default": DEFAULT_LIMIT,
        },
    },
    {
        "name": "offset",
        "in": "query",
        "required": False,
        "description": "Number of matching products to skip.",
        "schema": {
            "type": "integer",
            "minimum": MIN_OFFSET,
            "maximum": (1 << 63) - 1,
            "default": DEFAULT_OFFSET,
        },
    },
    {
        "name": "pagination",
        "in": "query",
        "required": False,
        "description": "Cursor cannot be combined with offset pagination.",
        "schema": {
            "type": "string",
            "enum": ["offset", "cursor"],
            "default": "offset",
        },
    },
    {
        "name": "cursor",
        "in": "query",
        "required": False,
        "description": "Opaque cursor returned by the previous cursor page.",
        "schema": {
            "type": "string",
            "pattern": "^[A-Za-z0-9_.-]+$",
            "maxLength": MAX_CURSOR_LENGTH,
        },
    },
]
_OPENAPI_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["openapi", "info", "paths", "components"],
    "properties": {
        "openapi": {"type": "string", "enum": ["3.0.3"]},
        "info": {
            "type": "object",
            "required": ["title", "version", "x-git-sha"],
            "properties": {
                "title": {"type": "string", "enum": ["agent-qa"]},
                "version": {"type": "string"},
                "x-git-sha": {"type": "string"},
            },
        },
        "paths": {"type": "object"},
        "components": {"type": "object"},
    },
}
_FIXTURE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "record_type": {"type": "string", "enum": ["synthetic_customer_fixture"]},
        "customer_id": _CUSTOMER_ID_SCHEMA,
        "name": {"type": "string"},
        "email": {"type": "string", "format": "email"},
        "plan": {"type": "string"},
        "is_real_person": {"type": "boolean"},
    },
    "additionalProperties": False,
}
_KEY_CREATION_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["key_id", "role", "label", "key", "created_at"],
    "properties": {
        "key_id": {"type": "string"},
        "role": {"type": "string", "enum": ["read", "write", "admin"]},
        "label": {"type": "string"},
        "key": {"type": "string"},
        "created_at": {"type": "string", "format": "date-time"},
    },
    "additionalProperties": False,
}
_KEY_LIST_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["items", "total"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "key_id",
                    "role",
                    "label",
                    "created_at",
                    "last_used_at",
                    "fingerprint",
                    "status",
                ],
                "properties": {
                    "key_id": {"type": "string"},
                    "role": {
                        "type": "string",
                        "enum": ["read", "write", "admin"],
                    },
                    "label": {"type": "string"},
                    "created_at": {"type": "string", "format": "date-time"},
                    "last_used_at": {
                        "type": "string",
                        "format": "date-time",
                        "nullable": True,
                    },
                    "fingerprint": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{8}$",
                    },
                    "status": {"type": "string", "enum": ["active", "grace"]},
                },
                "additionalProperties": False,
            },
        },
        "total": {"type": "integer", "minimum": 1, "maximum": 21},
    },
    "additionalProperties": False,
}

ROUTES = (
    {
        "method": "GET",
        "path": "/health",
        "handler": health,
        "role": None,
        "auth_required": False,
        "operation_id": "getHealth",
        "summary": "Check service health",
        "responses": ["200", "403"],
        "response_schemas": {
            "200": {
                "type": "object",
                "required": ["status"],
                "properties": {"status": {"type": "string", "enum": ["ok"]}},
                "additionalProperties": False,
            }
        },
    },
    {
        "method": "GET",
        "path": "/about",
        "handler": about,
        "role": None,
        "auth_required": False,
        "operation_id": "getAbout",
        "summary": "Read service name and build SHA",
        "responses": ["200", "403"],
        "response_schemas": {
            "200": {
                "type": "object",
                "required": ["service", "git_sha", "environment"],
                "properties": {
                    "service": {"type": "string", "enum": ["agent-qa"]},
                    "git_sha": {"type": "string"},
                    "environment": {"type": "string", "enum": ["qa"]},
                },
                "additionalProperties": False,
            }
        },
    },
    {
        "method": "GET",
        "path": "/status",
        "handler": status,
        "role": None,
        "auth_required": False,
        "operation_id": "getStatus",
        "summary": "Check service and fixture status",
        "responses": ["200", "403"],
        "response_schemas": {
            "200": {
                "type": "object",
                "required": ["status", "service", "checks"],
                "properties": {
                    "status": {"type": "string", "enum": ["ok", "degraded"]},
                    "service": {"type": "string", "enum": ["agent-qa"]},
                    "checks": {
                        "type": "object",
                        "required": ["fixture"],
                        "properties": {"fixture": {"type": "boolean"}},
                        "additionalProperties": False,
                    },
                },
                "additionalProperties": False,
            }
        },
    },
    {
        "method": "GET",
        "path": "/ready",
        "handler": ready,
        "role": None,
        "auth_required": False,
        "operation_id": "getReady",
        "summary": "Check service readiness",
        "responses": ["200", "403"],
        "response_schemas": {
            "200": {
                "type": "object",
                "required": ["status", "git_sha"],
                "properties": {
                    "status": {"type": "string", "enum": ["ready"]},
                    "git_sha": {"type": "string"},
                },
                "additionalProperties": False,
            }
        },
    },
    {
        "method": "GET",
        "path": "/ping",
        "handler": ping,
        "role": None,
        "auth_required": False,
        "operation_id": "getPing",
        "summary": "Check service liveness",
        "responses": ["200", "403"],
        "response_schemas": {
            "200": {
                "type": "object",
                "required": ["pong"],
                "properties": {"pong": {"type": "boolean", "enum": [True]}},
                "additionalProperties": False,
            }
        },
    },
    {
        "method": "GET",
        "path": "/metrics",
        "handler": metrics,
        "role": None,
        "auth_required": False,
        "operation_id": "getMetrics",
        "summary": "Read service metrics",
        "responses": ["200", "403"],
    },
    {
        "method": "GET",
        "path": "/fixture",
        "handler": fixture,
        "role": None,
        "auth_required": False,
        "operation_id": "getFixture",
        "summary": "Read the synthetic fixture",
        "parameters": [
            {
                "name": "fields",
                "in": "query",
                "required": False,
                "description": "Comma-separated fixture fields to return.",
                "schema": {"type": "string"},
            }
        ],
        "responses": ["200", "400", "403"],
        "response_schemas": {"200": _FIXTURE_RESPONSE_SCHEMA},
    },
    {
        "method": "GET",
        "path": "/version",
        "handler": version,
        "role": None,
        "auth_required": False,
        "operation_id": "getVersion",
        "summary": "Read service version details",
        "responses": ["200", "403"],
        "response_schemas": {
            "200": {
                "type": "object",
                "required": ["service", "git_sha", "python_version"],
                "properties": {
                    "service": {"type": "string", "enum": ["agent-qa"]},
                    "git_sha": {"type": "string"},
                    "python_version": {"type": "string"},
                },
                "additionalProperties": False,
            }
        },
    },
    {
        "method": "GET",
        "path": "/openapi.json",
        "handler": openapi,
        "role": None,
        "auth_required": False,
        "operation_id": "getOpenapi",
        "summary": "Read the OpenAPI document",
        "responses": ["200", "403"],
        "response_schemas": {"200": _OPENAPI_RESPONSE_SCHEMA},
    },
    {
        "method": "GET",
        "path": "/schemas",
        "handler": list_schemas,
        "role": None,
        "auth_required": False,
        "operation_id": "listSchemas",
        "summary": "List registered JSON schemas",
        "responses": ["200", "403"],
        "response_schemas": {
            "200": {
                "type": "object",
                "required": ["items"],
                "properties": {"items": {"type": "array", "items": {"type": "string"}}},
                "additionalProperties": False,
            }
        },
    },
    {
        "method": "GET",
        "path": "/schemas/{name}",
        "handler": get_schema,
        "role": None,
        "auth_required": False,
        "operation_id": "getSchema",
        "summary": "Read a named JSON schema",
        "parameters": [
            {
                "name": "name",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
        ],
        "responses": ["200", "404", "403"],
        "response_schemas": {"200": {"type": "object"}},
    },
    {
        "method": "POST",
        "path": "/schemas/{name}/validate",
        "handler": validate_named_schema,
        "body": True,
        "json_object_only": False,
        "request_schema": {},
        "role": None,
        "auth_required": False,
        "operation_id": "validateNamedSchema",
        "summary": "Validate a JSON value against a named schema",
        "parameters": [
            {
                "name": "name",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
        ],
        "responses": ["200", "400", "404", "411", "413", "415", "403"],
        "response_schemas": {
            "200": {
                "type": "object",
                "required": ["valid", "errors"],
                "properties": {
                    "valid": {"type": "boolean"},
                    "errors": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["field", "message"],
                            "properties": {
                                "field": {"type": "string"},
                                "message": {"type": "string"},
                            },
                        },
                    },
                },
            }
        },
    },
    {
        "method": "GET",
        "path": "/exports/products.csv",
        "handler": export_products_csv,
        "role": "read",
        "auth_required": True,
        "operation_id": "exportProductsCsv",
        "summary": "Export filtered products as CSV",
        "produces": ["text/csv"],
        "parameters": _EXPORT_PRODUCT_QUERY,
        "responses": ["200", "400", "401", "406", "403"],
        "response_schemas": {"200": {"type": "string", "format": "binary"}},
        "response_headers": {
            "200": {
                "Content-Disposition": {
                    "description": "Download filename.",
                    "schema": {"type": "string"},
                }
            }
        },
    },
    {
        "method": "GET",
        "path": "/exports/orders.csv",
        "handler": export_orders_csv,
        "role": "read",
        "auth_required": True,
        "operation_id": "exportOrdersCsv",
        "summary": "Export filtered orders as CSV",
        "produces": ["text/csv"],
        "parameters": _EXPORT_ORDER_QUERY,
        "responses": ["200", "400", "401", "406", "403"],
        "response_schemas": {"200": {"type": "string", "format": "binary"}},
        "response_headers": {
            "200": {
                "Content-Disposition": {
                    "description": "Download filename.",
                    "schema": {"type": "string"},
                }
            }
        },
    },
    {
        "method": "POST",
        "path": "/imports/products",
        "handler": import_products_csv,
        "body": True,
        "max_body_bytes": 65536,
        "consumes": ["text/csv"],
        "role": "write",
        "auth_required": True,
        "operation_id": "importProductsCsv",
        "summary": "Import products from CSV",
        "parameters": _CSV_IMPORT_QUERY,
        "responses": ["200", "201", "400", "401", "411", "413", "415", "422", "403"],
        "response_schemas": {
            "200": _CSV_IMPORT_RESPONSE_SCHEMA,
            "201": _CSV_IMPORT_RESPONSE_SCHEMA,
            "422": _CSV_IMPORT_RESPONSE_SCHEMA,
        },
        "error_responses": {"400": "Invalid CSV structure or import options."},
    },
    {
        "method": "POST",
        "path": "/imports/orders",
        "handler": import_orders_csv,
        "body": True,
        "max_body_bytes": 65536,
        "consumes": ["text/csv"],
        "role": "write",
        "auth_required": True,
        "operation_id": "importOrdersCsv",
        "summary": "Import legacy orders from CSV",
        "parameters": _CSV_IMPORT_QUERY,
        "responses": ["200", "201", "400", "401", "411", "413", "415", "422", "403"],
        "response_schemas": {
            "200": _CSV_IMPORT_RESPONSE_SCHEMA,
            "201": _CSV_IMPORT_RESPONSE_SCHEMA,
            "422": _CSV_IMPORT_RESPONSE_SCHEMA,
        },
        "error_responses": {"400": "Invalid CSV structure or import options."},
    },
    {
        "method": "GET",
        "path": "/orders",
        "handler": list_orders,
        "role": None,
        "auth_required": False,
        "operation_id": "listOrders",
        "summary": "List orders",
        "parameters": [
            _IF_NONE_MATCH,
            {
                "name": "status",
                "in": "query",
                "required": False,
                "description": "Filter by order status.",
                "schema": _STATUS_SCHEMA,
            },
            {
                "name": "customer_id",
                "in": "query",
                "required": False,
                "description": "Filter by customer identifier.",
                "schema": {"type": "string"},
            },
            {
                "name": "sort",
                "in": "query",
                "required": False,
                "description": "Order ordering; id or -id.",
                "schema": {"type": "string", "enum": ["id", "-id"], "default": "id"},
            },
            {
                "name": "limit",
                "in": "query",
                "required": False,
                "description": "Maximum number of orders to return.",
                "schema": {
                    "type": "integer",
                    "minimum": MIN_LIMIT,
                    "maximum": MAX_LIMIT,
                    "default": DEFAULT_LIMIT,
                },
            },
            {
                "name": "offset",
                "in": "query",
                "required": False,
                "description": "Number of matching orders to skip.",
                "schema": {
                    "type": "integer",
                    "minimum": MIN_OFFSET,
                    "maximum": (1 << 63) - 1,
                    "default": DEFAULT_OFFSET,
                },
            },
            {
                "name": "pagination",
                "in": "query",
                "required": False,
                "description": "Cursor cannot be combined with offset pagination.",
                "schema": {
                    "type": "string",
                    "enum": ["offset", "cursor"],
                    "default": "offset",
                },
            },
            {
                "name": "cursor",
                "in": "query",
                "required": False,
                "description": "Opaque cursor returned by the previous cursor page.",
                "schema": {
                    "type": "string",
                    "pattern": "^[A-Za-z0-9_.-]+$",
                    "maxLength": MAX_CURSOR_LENGTH,
                },
            },
        ],
        "responses": ["200", "304", "400", "403"],
        "response_schemas": {"200": _ORDER_LIST_RESPONSE_SCHEMA},
        "conditional_headers": ["If-None-Match"],
        "error_responses": {
            "400": (
                "Invalid query (including conflicting cursor and offset), "
                "invalid_cursor, or cursor_mismatch."
            )
        },
        "response_headers": {
            "200": {
                "Link": {
                    "description": (
                        "Relative link to the next cursor page, when present."
                    ),
                    "schema": {"type": "string"},
                }
            }
        },
    },
    {
        "method": "POST",
        "path": "/orders",
        "handler": create_order,
        "body": True,
        "role": "write",
        "auth_required": True,
        "operation_id": "createOrder",
        "summary": "Create an order",
        "idempotent": True,
        "etag_response": True,
        "request_schema": _CREATE_ORDER_SCHEMA,
        "responses": ["201", "400", "401", "409", "411", "413", "415", "422", "403"],
        "response_schemas": {"201": _ORDER_RESPONSE_SCHEMA},
    },
    {
        "method": "POST",
        "path": "/orders/bulk",
        "handler": create_orders_bulk,
        "body": True,
        "role": "write",
        "auth_required": True,
        "operation_id": "createOrdersBulk",
        "summary": "Create orders in bulk",
        "idempotent": True,
        "idempotency_replay_statuses": [422],
        "max_body_bytes": 65536,
        "request_schema": SCHEMAS["CreateOrdersBulk"],
        "responses": [
            "201",
            "207",
            "400",
            "401",
            "409",
            "411",
            "413",
            "415",
            "422",
            "403",
        ],
        "response_schemas": {
            "201": {"$ref": "#/components/schemas/BulkCreateResponse"},
            "207": {"$ref": "#/components/schemas/BulkCreateResponse"},
            "422": {"$ref": "#/components/schemas/BulkCreateResponse"},
        },
    },
    {
        "method": "DELETE",
        "path": "/orders/{id}",
        "handler": delete_order,
        "role": "write",
        "auth_required": True,
        "operation_id": "deleteOrder",
        "summary": "Delete an order",
        "parameters": [_ORDER_ID, _IF_MATCH],
        "responses": ["204", "400", "401", "404", "412", "428", "403"],
        "conditional_headers": ["If-Match"],
    },
    {
        "method": "GET",
        "path": "/orders/{id}",
        "handler": get_order,
        "role": None,
        "auth_required": False,
        "operation_id": "getOrder",
        "summary": "Read an order",
        "parameters": [_ORDER_ID, _IF_NONE_MATCH],
        "responses": ["200", "304", "400", "404", "403"],
        "response_schemas": {"200": _ORDER_RESPONSE_SCHEMA},
        "conditional_headers": ["If-None-Match"],
    },
    {
        "method": "PATCH",
        "path": "/orders/{id}",
        "handler": patch_order,
        "body": True,
        "role": "write",
        "auth_required": True,
        "operation_id": "updateOrder",
        "summary": "Update an order",
        "parameters": [_ORDER_ID, _IF_MATCH],
        "request_schema": _UPDATE_ORDER_SCHEMA,
        "responses": [
            "200",
            "400",
            "401",
            "404",
            "409",
            "412",
            "413",
            "415",
            "428",
            "403",
        ],
        "response_schemas": {"200": _ORDER_RESPONSE_SCHEMA},
        "conditional_headers": ["If-Match"],
    },
    {
        "method": "POST",
        "path": "/products",
        "handler": create_product,
        "body": True,
        "role": "write",
        "auth_required": True,
        "operation_id": "createProduct",
        "summary": "Create a product",
        "idempotent": True,
        "etag_response": True,
        "request_schema": _CREATE_PRODUCT_SCHEMA,
        "responses": ["201", "400", "401", "409", "411", "413", "415", "422", "403"],
        "response_schemas": {"201": _PRODUCT_RESPONSE_SCHEMA},
    },
    {
        "method": "POST",
        "path": "/products/bulk",
        "handler": create_products_bulk,
        "body": True,
        "role": "write",
        "auth_required": True,
        "operation_id": "createProductsBulk",
        "summary": "Create products in bulk",
        "idempotent": True,
        "idempotency_replay_statuses": [422],
        "max_body_bytes": 65536,
        "request_schema": SCHEMAS["CreateProductsBulk"],
        "responses": [
            "201",
            "207",
            "400",
            "401",
            "409",
            "411",
            "413",
            "415",
            "422",
            "403",
        ],
        "response_schemas": {
            "201": {"$ref": "#/components/schemas/BulkCreateResponse"},
            "207": {"$ref": "#/components/schemas/BulkCreateResponse"},
            "422": {"$ref": "#/components/schemas/BulkCreateResponse"},
        },
    },
    {
        "method": "GET",
        "path": "/products",
        "handler": list_products,
        "role": None,
        "auth_required": False,
        "operation_id": "listProducts",
        "summary": "Search and list products",
        "parameters": [*_PRODUCT_QUERY_PARAMETERS, _IF_NONE_MATCH],
        "responses": ["200", "304", "400", "403"],
        "response_schemas": {"200": _PRODUCT_LIST_RESPONSE_SCHEMA},
        "conditional_headers": ["If-None-Match"],
        "error_responses": {
            "400": (
                "Invalid query (including conflicting cursor and offset), "
                "invalid_cursor, or cursor_mismatch."
            )
        },
        "response_headers": {
            "200": {
                "Link": {
                    "description": (
                        "Relative link to the next cursor page, when present."
                    ),
                    "schema": {"type": "string"},
                }
            }
        },
    },
    {
        "method": "DELETE",
        "path": "/products/{id}",
        "handler": delete_product,
        "role": "write",
        "auth_required": True,
        "operation_id": "deleteProduct",
        "summary": "Delete a product",
        "parameters": [_PRODUCT_ID, _IF_MATCH],
        "responses": ["204", "400", "401", "404", "412", "428", "403"],
        "conditional_headers": ["If-Match"],
    },
    {
        "method": "GET",
        "path": "/products/{id}",
        "handler": get_product,
        "role": None,
        "auth_required": False,
        "operation_id": "getProduct",
        "summary": "Read a product",
        "parameters": [_PRODUCT_ID, _IF_NONE_MATCH],
        "responses": ["200", "304", "400", "404", "403"],
        "response_schemas": {"200": _PRODUCT_RESPONSE_SCHEMA},
        "conditional_headers": ["If-None-Match"],
    },
    {
        "method": "PATCH",
        "path": "/products/{id}",
        "handler": patch_product,
        "body": True,
        "role": "write",
        "auth_required": True,
        "operation_id": "updateProduct",
        "summary": "Update a product",
        "parameters": [_PRODUCT_ID, _IF_MATCH],
        "request_schema": _UPDATE_PRODUCT_SCHEMA,
        "responses": [
            "200",
            "400",
            "401",
            "404",
            "409",
            "411",
            "412",
            "413",
            "415",
            "428",
            "403",
        ],
        "response_schemas": {"200": _PRODUCT_RESPONSE_SCHEMA},
        "conditional_headers": ["If-Match"],
    },
    {
        "method": "POST",
        "path": "/products/{id}/adjust-stock",
        "handler": adjust_product_stock,
        "body": True,
        "role": "write",
        "auth_required": True,
        "operation_id": "adjustProductStock",
        "summary": "Adjust product stock atomically",
        "parameters": [_PRODUCT_ID, _IF_MATCH],
        "request_schema": _ADJUST_STOCK_SCHEMA,
        "responses": [
            "200",
            "400",
            "401",
            "404",
            "409",
            "411",
            "412",
            "413",
            "415",
            "428",
            "403",
        ],
        "response_schemas": {"200": _PRODUCT_RESPONSE_SCHEMA},
        "conditional_headers": ["If-Match"],
    },
    {
        "method": "GET",
        "path": "/whoami",
        "handler": whoami,
        "role": "read",
        "auth_required": True,
        "pass_identity": True,
        "operation_id": "getWhoami",
        "summary": "Read the authenticated API key identity",
        "responses": ["200", "401", "403"],
        "response_schemas": {
            "200": {
                "type": "object",
                "required": ["key_id", "role", "label"],
                "properties": {
                    "key_id": {"type": "string"},
                    "role": {"type": "string", "enum": ["read", "write", "admin"]},
                    "label": {"type": "string"},
                },
                "additionalProperties": False,
            }
        },
    },
    {
        "method": "POST",
        "path": "/admin/keys",
        "handler": create_api_key,
        "body": True,
        "sanitize_validation_errors": True,
        "role": "admin",
        "auth_required": True,
        "operation_id": "createApiKey",
        "summary": "Create an API key",
        "request_schema": {
            "type": "object",
            "required": ["role", "label"],
            "properties": {
                "role": {
                    "type": "string",
                    "enum": ["read", "write", "admin"],
                },
                "label": {"type": "string", "minLength": 1, "maxLength": 40},
            },
            "additionalProperties": False,
        },
        "responses": ["201", "400", "401", "403", "409", "411", "413", "415"],
        "response_schemas": {"201": _KEY_CREATION_RESPONSE_SCHEMA},
        "error_responses": {
            "400": "Invalid role or label (validation_error or invalid_label).",
            "409": "The active key limit has been reached (key_limit).",
        },
    },
    {
        "method": "GET",
        "path": "/admin/keys",
        "handler": list_api_keys,
        "role": "admin",
        "auth_required": True,
        "operation_id": "listApiKeys",
        "summary": "List API keys without exposing secrets",
        "responses": ["200", "401", "403"],
        "response_schemas": {"200": _KEY_LIST_RESPONSE_SCHEMA},
    },
    {
        "method": "POST",
        "path": "/admin/keys/{key_id}/rotate",
        "handler": rotate_api_key,
        "body": True,
        "sanitize_validation_errors": True,
        "role": "admin",
        "auth_required": True,
        "operation_id": "rotateApiKey",
        "summary": "Rotate an API key, optionally allowing a grace period",
        "parameters": [
            {
                "name": "key_id",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "maxLength": 32},
            }
        ],
        "request_schema": {
            "type": "object",
            "properties": {
                "grace_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 300,
                }
            },
            "additionalProperties": False,
        },
        "responses": ["200", "400", "401", "403", "404", "409", "411", "413", "415"],
        "response_schemas": {"200": _KEY_CREATION_RESPONSE_SCHEMA},
        "error_responses": {
            "400": "Invalid grace_seconds (validation_error).",
            "404": "The key does not exist (key_not_found).",
            "409": "The bootstrap key is immutable (bootstrap_key_immutable).",
        },
    },
    {
        "method": "DELETE",
        "path": "/admin/keys/{key_id}",
        "handler": revoke_api_key,
        "role": "admin",
        "auth_required": True,
        "operation_id": "revokeApiKey",
        "summary": "Revoke an API key",
        "parameters": [
            {
                "name": "key_id",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "maxLength": 32},
            }
        ],
        "responses": ["204", "401", "403", "404", "409"],
        "error_responses": {
            "404": "The key does not exist (key_not_found).",
            "409": "The bootstrap key is immutable (bootstrap_key_immutable).",
        },
    },
    {
        "method": "GET",
        "path": "/categories",
        "handler": categories,
        "role": None,
        "auth_required": False,
        "operation_id": "listCategories",
        "summary": "Read product category aggregates",
        "responses": ["200", "403"],
        "response_schemas": {"200": _CATEGORY_LIST_RESPONSE_SCHEMA},
    },
    {
        "method": "GET",
        "path": "/audit",
        "handler": list_audit,
        "role": "admin",
        "auth_required": True,
        "operation_id": "listAuditEntries",
        "summary": "Filter retained audit entries",
        "parameters": _AUDIT_QUERY_PARAMETERS,
        "responses": ["200", "400", "401", "403"],
        "response_schemas": {"200": _AUDIT_LIST_RESPONSE_SCHEMA},
        "error_responses": {
            "400": "Invalid, unknown, or repeated query parameter (invalid_query)."
        },
    },
    {
        "method": "GET",
        "path": "/audit/{seq}",
        "handler": get_audit_entry,
        "role": "admin",
        "auth_required": True,
        "operation_id": "getAuditEntry",
        "summary": "Read one retained audit entry",
        "parameters": [
            {
                "name": "seq",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": (1 << 63) - 1,
                },
            }
        ],
        "responses": ["200", "401", "403", "404"],
        "response_schemas": {"200": _AUDIT_ENTRY_SCHEMA},
        "error_responses": {
            "404": "The sequence is not retained (audit_entry_not_found)."
        },
    },
)
