"""Pure route handlers for the agent QA HTTP service."""

import json
import platform

from agent_qa.config import FIXTURE_PATH, GIT_SHA
from agent_qa.errors import ApiError
from agent_qa.metrics import REGISTRY
from agent_qa.orders import OrderStore, validate_create, validate_patch, validate_query


ORDER_STORE = OrderStore()


def ready(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return the service readiness document."""
    return 200, {"status": "ready", "git_sha": GIT_SHA}, {}


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


ROUTES = (
    {"method": "GET", "path": "/ready", "handler": ready, "auth_required": False},
    {"method": "GET", "path": "/metrics", "handler": metrics, "auth_required": False},
    {"method": "GET", "path": "/fixture", "handler": fixture, "auth_required": False},
    {"method": "GET", "path": "/version", "handler": version, "auth_required": False},
    {
        "method": "GET",
        "path": "/orders",
        "handler": list_orders,
        "auth_required": False,
    },
    {
        "method": "POST",
        "path": "/orders",
        "handler": create_order,
        "body": True,
        "auth_required": True,
    },
    {
        "method": "DELETE",
        "path": "/orders/{id}",
        "handler": delete_order,
        "auth_required": True,
    },
    {
        "method": "GET",
        "path": "/orders/{id}",
        "handler": get_order,
        "auth_required": False,
    },
    {
        "method": "PATCH",
        "path": "/orders/{id}",
        "handler": patch_order,
        "body": True,
        "auth_required": True,
    },
)
