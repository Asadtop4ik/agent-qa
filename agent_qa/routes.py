"""Pure route handlers for the agent QA HTTP service."""

import hashlib
import json
import os
import platform

from agent_qa.config import FIXTURE_PATH, GIT_SHA
from agent_qa.conditional import etag_for, parse_etag_list, weak_match
from agent_qa.errors import ApiError
from agent_qa.fulfillment import FulfillmentService
from agent_qa.metrics import REGISTRY
from agent_qa.openapi import build_openapi
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
from agent_qa.validation import validate
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
    return (
        200,
        REGISTRY.render(order_count, GIT_SHA, products=product_count),
        {"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
    )


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


def list_orders(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
    request_headers: dict[str, str] | None = None,
) -> tuple[int, object, dict[str, str]]:
    """Return filtered and paginated orders."""
    filters = validate_query(query)
    items, total = ORDER_STORE.list(**filters)
    body = {
        "items": items,
        "total": total,
        "limit": filters["limit"],
        "offset": filters["offset"],
    }
    etag = _list_etag(body)
    headers = {"ETag": etag}
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


def list_products(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
    request_headers: dict[str, str] | None = None,
) -> tuple[int, object, dict[str, str]]:
    """Return filtered and paginated products."""
    filters = validate_product_query(query)
    items, total = PRODUCT_STORE.list(**filters)
    body = {
        "items": items,
        "total": total,
        "limit": filters["limit"],
        "offset": filters["offset"],
    }
    etag = _list_etag(body)
    headers = {"ETag": etag}
    if _if_none_match(request_headers, etag):
        return 304, None, headers
    return 200, body, headers


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
            "default": DEFAULT_OFFSET,
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

ROUTES = (
    {
        "method": "GET",
        "path": "/health",
        "handler": health,
        "auth_required": False,
        "operation_id": "getHealth",
        "summary": "Check service health",
        "responses": ["200"],
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
        "auth_required": False,
        "operation_id": "getAbout",
        "summary": "Read service name and build SHA",
        "responses": ["200"],
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
        "auth_required": False,
        "operation_id": "getStatus",
        "summary": "Check service and fixture status",
        "responses": ["200"],
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
        "auth_required": False,
        "operation_id": "getReady",
        "summary": "Check service readiness",
        "responses": ["200"],
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
        "auth_required": False,
        "operation_id": "getPing",
        "summary": "Check service liveness",
        "responses": ["200"],
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
        "auth_required": False,
        "operation_id": "getMetrics",
        "summary": "Read service metrics",
        "responses": ["200"],
    },
    {
        "method": "GET",
        "path": "/fixture",
        "handler": fixture,
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
        "responses": ["200", "400"],
        "response_schemas": {"200": _FIXTURE_RESPONSE_SCHEMA},
    },
    {
        "method": "GET",
        "path": "/version",
        "handler": version,
        "auth_required": False,
        "operation_id": "getVersion",
        "summary": "Read service version details",
        "responses": ["200"],
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
        "auth_required": False,
        "operation_id": "getOpenapi",
        "summary": "Read the OpenAPI document",
        "responses": ["200"],
        "response_schemas": {"200": _OPENAPI_RESPONSE_SCHEMA},
    },
    {
        "method": "GET",
        "path": "/schemas",
        "handler": list_schemas,
        "auth_required": False,
        "operation_id": "listSchemas",
        "summary": "List registered JSON schemas",
        "responses": ["200"],
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
        "responses": ["200", "404"],
        "response_schemas": {"200": {"type": "object"}},
    },
    {
        "method": "POST",
        "path": "/schemas/{name}/validate",
        "handler": validate_named_schema,
        "body": True,
        "json_object_only": False,
        "request_schema": {},
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
        "responses": ["200", "400", "404", "411", "413", "415"],
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
        "path": "/orders",
        "handler": list_orders,
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
                    "default": DEFAULT_OFFSET,
                },
            },
        ],
        "responses": ["200", "304", "400"],
        "response_schemas": {"200": _ORDER_LIST_RESPONSE_SCHEMA},
        "conditional_headers": ["If-None-Match"],
    },
    {
        "method": "POST",
        "path": "/orders",
        "handler": create_order,
        "body": True,
        "auth_required": True,
        "operation_id": "createOrder",
        "summary": "Create an order",
        "idempotent": True,
        "etag_response": True,
        "request_schema": _CREATE_ORDER_SCHEMA,
        "responses": ["201", "400", "401", "409", "411", "413", "415", "422"],
        "response_schemas": {"201": _ORDER_RESPONSE_SCHEMA},
    },
    {
        "method": "DELETE",
        "path": "/orders/{id}",
        "handler": delete_order,
        "auth_required": True,
        "operation_id": "deleteOrder",
        "summary": "Delete an order",
        "parameters": [_ORDER_ID, _IF_MATCH],
        "responses": ["204", "400", "401", "404", "412", "428"],
        "conditional_headers": ["If-Match"],
    },
    {
        "method": "GET",
        "path": "/orders/{id}",
        "handler": get_order,
        "auth_required": False,
        "operation_id": "getOrder",
        "summary": "Read an order",
        "parameters": [_ORDER_ID, _IF_NONE_MATCH],
        "responses": ["200", "304", "400", "404"],
        "response_schemas": {"200": _ORDER_RESPONSE_SCHEMA},
        "conditional_headers": ["If-None-Match"],
    },
    {
        "method": "PATCH",
        "path": "/orders/{id}",
        "handler": patch_order,
        "body": True,
        "auth_required": True,
        "operation_id": "updateOrder",
        "summary": "Update an order",
        "parameters": [_ORDER_ID, _IF_MATCH],
        "request_schema": _UPDATE_ORDER_SCHEMA,
        "responses": ["200", "400", "401", "404", "409", "412", "413", "415", "428"],
        "response_schemas": {"200": _ORDER_RESPONSE_SCHEMA},
        "conditional_headers": ["If-Match"],
    },
    {
        "method": "POST",
        "path": "/products",
        "handler": create_product,
        "body": True,
        "auth_required": True,
        "operation_id": "createProduct",
        "summary": "Create a product",
        "idempotent": True,
        "etag_response": True,
        "request_schema": _CREATE_PRODUCT_SCHEMA,
        "responses": ["201", "400", "401", "409", "411", "413", "415", "422"],
        "response_schemas": {"201": _PRODUCT_RESPONSE_SCHEMA},
    },
    {
        "method": "GET",
        "path": "/products",
        "handler": list_products,
        "auth_required": False,
        "operation_id": "listProducts",
        "summary": "Search and list products",
        "parameters": [*_PRODUCT_QUERY_PARAMETERS, _IF_NONE_MATCH],
        "responses": ["200", "304", "400"],
        "response_schemas": {"200": _PRODUCT_LIST_RESPONSE_SCHEMA},
        "conditional_headers": ["If-None-Match"],
    },
    {
        "method": "DELETE",
        "path": "/products/{id}",
        "handler": delete_product,
        "auth_required": True,
        "operation_id": "deleteProduct",
        "summary": "Delete a product",
        "parameters": [_PRODUCT_ID, _IF_MATCH],
        "responses": ["204", "400", "401", "404", "412", "428"],
        "conditional_headers": ["If-Match"],
    },
    {
        "method": "GET",
        "path": "/products/{id}",
        "handler": get_product,
        "auth_required": False,
        "operation_id": "getProduct",
        "summary": "Read a product",
        "parameters": [_PRODUCT_ID, _IF_NONE_MATCH],
        "responses": ["200", "304", "400", "404"],
        "response_schemas": {"200": _PRODUCT_RESPONSE_SCHEMA},
        "conditional_headers": ["If-None-Match"],
    },
    {
        "method": "PATCH",
        "path": "/products/{id}",
        "handler": patch_product,
        "body": True,
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
        ],
        "response_schemas": {"200": _PRODUCT_RESPONSE_SCHEMA},
        "conditional_headers": ["If-Match"],
    },
    {
        "method": "POST",
        "path": "/products/{id}/adjust-stock",
        "handler": adjust_product_stock,
        "body": True,
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
        ],
        "response_schemas": {"200": _PRODUCT_RESPONSE_SCHEMA},
        "conditional_headers": ["If-Match"],
    },
    {
        "method": "GET",
        "path": "/categories",
        "handler": categories,
        "auth_required": False,
        "operation_id": "listCategories",
        "summary": "Read product category aggregates",
        "responses": ["200"],
        "response_schemas": {"200": _CATEGORY_LIST_RESPONSE_SCHEMA},
    },
)
