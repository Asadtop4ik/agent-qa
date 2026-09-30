"""Pure route handlers for the agent QA HTTP service."""

import json
import platform

from agent_qa.config import FIXTURE_PATH, GIT_SHA
from agent_qa.errors import ApiError
from agent_qa.metrics import REGISTRY
from agent_qa.openapi import build_openapi
from agent_qa.orders import (
    DEFAULT_LIMIT,
    DEFAULT_OFFSET,
    MAX_CUSTOMER_ID_LENGTH,
    MAX_LIMIT,
    MAX_TOTAL_CENTS,
    MIN_CUSTOMER_ID_LENGTH,
    MIN_LIMIT,
    MIN_OFFSET,
    MIN_TOTAL_CENTS,
    STATUSES,
    OrderStore,
    validate_create,
    validate_patch,
    validate_query,
)


ORDER_STORE = OrderStore()


def ready(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return the service readiness document."""
    return 200, {"status": "ready", "git_sha": GIT_SHA}, {}


def ping(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return a simple liveness response."""
    return 200, {"pong": True}, {}


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
    return (
        200,
        REGISTRY.render(order_count, GIT_SHA),
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


def create_order(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Validate and create an order."""
    values = validate_create(payload)
    order = ORDER_STORE.create(**values)
    return 201, order, {"Location": f"/orders/{order['id']}"}


def list_orders(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return filtered and paginated orders."""
    filters = validate_query(query)
    items, total = ORDER_STORE.list(**filters)
    return (
        200,
        {
            "items": items,
            "total": total,
            "limit": filters["limit"],
            "offset": filters["offset"],
        },
        {},
    )


def _order_id(path_params: dict[str, str] | None) -> int | None:
    raw_id = (path_params or {}).get("id", "")
    if not raw_id.isascii() or not raw_id.isdigit():
        return None
    order_id = int(raw_id)
    return order_id if order_id > 0 else None


def get_order(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return one order or the standard missing-order error."""
    order_id = _order_id(path_params)
    order = ORDER_STORE.get(order_id) if order_id is not None else None
    if order is None:
        raise ApiError(404, "order_not_found", "Order not found")
    return 200, order, {}


def patch_order(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Validate and update an order."""
    order_id = _order_id(path_params)
    if order_id is None:
        raise ApiError(404, "order_not_found", "Order not found")
    changes = validate_patch(payload)
    order = ORDER_STORE.update(order_id, changes)
    if order is None:
        raise ApiError(404, "order_not_found", "Order not found")
    return 200, order, {}


def delete_order(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Delete an order and return an empty response body."""
    order_id = _order_id(path_params)
    if order_id is None or not ORDER_STORE.delete(order_id):
        raise ApiError(404, "order_not_found", "Order not found")
    return 204, None, {"Content-Length": "0"}


_ORDER_ID = {
    "name": "id",
    "in": "path",
    "required": True,
    "description": "Positive order identifier.",
    "schema": {"type": "integer", "minimum": 1},
}
_CUSTOMER_ID_SCHEMA = {
    "type": "string",
    "minLength": MIN_CUSTOMER_ID_LENGTH,
    "maxLength": MAX_CUSTOMER_ID_LENGTH,
}
_TOTAL_CENTS_SCHEMA = {
    "type": "integer",
    "minimum": MIN_TOTAL_CENTS,
    "maximum": MAX_TOTAL_CENTS,
}
_STATUS_SCHEMA = {"type": "string", "enum": list(STATUSES)}
_CREATE_ORDER_SCHEMA = {
    "type": "object",
    "required": ["customer_id", "total_cents"],
    "properties": {
        "customer_id": _CUSTOMER_ID_SCHEMA,
        "total_cents": _TOTAL_CENTS_SCHEMA,
    },
    "additionalProperties": False,
}
_UPDATE_ORDER_SCHEMA = {
    "type": "object",
    "properties": {
        "status": _STATUS_SCHEMA,
        "total_cents": _TOTAL_CENTS_SCHEMA,
    },
    "minProperties": 1,
    "additionalProperties": False,
}
_ORDER_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["id", "customer_id", "total_cents", "status", "created_at"],
    "properties": {
        "id": {"type": "integer", "minimum": 1},
        "customer_id": _CUSTOMER_ID_SCHEMA,
        "total_cents": _TOTAL_CENTS_SCHEMA,
        "status": _STATUS_SCHEMA,
        "created_at": {"type": "string", "format": "date-time"},
    },
    "additionalProperties": False,
}
_ORDER_LIST_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["items", "total", "limit", "offset"],
    "properties": {
        "items": {"type": "array", "items": _ORDER_RESPONSE_SCHEMA},
        "total": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": MIN_LIMIT, "maximum": MAX_LIMIT},
        "offset": {"type": "integer", "minimum": MIN_OFFSET},
    },
    "additionalProperties": False,
}
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
        "path": "/orders",
        "handler": list_orders,
        "auth_required": False,
        "operation_id": "listOrders",
        "summary": "List orders",
        "parameters": [
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
        "responses": ["200", "400"],
        "response_schemas": {"200": _ORDER_LIST_RESPONSE_SCHEMA},
    },
    {
        "method": "POST",
        "path": "/orders",
        "handler": create_order,
        "body": True,
        "auth_required": True,
        "operation_id": "createOrder",
        "summary": "Create an order",
        "request_schema": _CREATE_ORDER_SCHEMA,
        "responses": ["201", "400", "401", "409", "411", "413", "415"],
        "response_schemas": {"201": _ORDER_RESPONSE_SCHEMA},
    },
    {
        "method": "DELETE",
        "path": "/orders/{id}",
        "handler": delete_order,
        "auth_required": True,
        "operation_id": "deleteOrder",
        "summary": "Delete an order",
        "parameters": [_ORDER_ID],
        "responses": ["204", "401", "404"],
    },
    {
        "method": "GET",
        "path": "/orders/{id}",
        "handler": get_order,
        "auth_required": False,
        "operation_id": "getOrder",
        "summary": "Read an order",
        "parameters": [_ORDER_ID],
        "responses": ["200", "404"],
        "response_schemas": {"200": _ORDER_RESPONSE_SCHEMA},
    },
    {
        "method": "PATCH",
        "path": "/orders/{id}",
        "handler": patch_order,
        "body": True,
        "auth_required": True,
        "operation_id": "updateOrder",
        "summary": "Update an order",
        "parameters": [_ORDER_ID],
        "request_schema": _UPDATE_ORDER_SCHEMA,
        "responses": ["200", "400", "401", "404", "409", "413", "415"],
        "response_schemas": {"200": _ORDER_RESPONSE_SCHEMA},
    },
)
