"""Pure route handlers for the agent QA HTTP service."""

from copy import deepcopy
import hashlib
import json
import math
import platform
import re
from urllib.parse import quote, urlencode

from agent_qa import auth, settings
from agent_qa.audit import AUDIT_LOG
from agent_qa.config import FIXTURE_PATH, GIT_SHA
from agent_qa.conditional import etag_for, parse_etag_list, weak_match
from agent_qa.csvio import ORDER_COLUMNS, PRODUCT_COLUMNS, parse_csv, render_csv
from agent_qa.context import get_context
from agent_qa.errors import ApiError
from agent_qa.fulfillment import FulfillmentService
from agent_qa.metrics import REGISTRY
from agent_qa.outbox import OUTBOX
from agent_qa.openapi import build_openapi
from agent_qa.tracing import span as trace_span
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
    STATUSES,
    V2_CREATE_ORDER_SCHEMA,
    V2_ORDER_LIST_SCHEMA,
    V2_ORDER_SCHEMA,
    V2_UPDATE_ORDER_SCHEMA,
)
from agent_qa.searchdsl import (
    FIELD_METADATA,
    compile_predicate,
    count_terms,
    normalize,
    parse,
    supported_operators,
)
from agent_qa.validation import validate, validate_query_params
from agent_qa.versioning import versions_document
from agent_qa.orders import (
    OrderError,
    OrderStore,
    validate_create,
    validate_patch,
    validate_query,
)
from agent_qa.jobs import (
    JOB_STATUSES,
    JOB_TYPES,
    JobRunner,
)
from agent_qa.products import (
    ProductStore,
    validate_adjust_stock,
    validate_create as validate_product_create,
    validate_patch as validate_product_patch,
    validate_query as validate_product_query,
)
from agent_qa.ratelimit import RATE_LIMITER
from agent_qa import tenants


ORDER_STORE = tenants.get("default").orders
PRODUCT_STORE = tenants.get("default").products
JOB_RUNNER = tenants.get("default").jobs


def _tenant_name() -> str:
    context = get_context()
    return context.tenant if context is not None else "default"


def _order_store() -> OrderStore:
    return ORDER_STORE if _tenant_name() == "default" else tenants.current().orders


def _product_store() -> ProductStore:
    return PRODUCT_STORE if _tenant_name() == "default" else tenants.current().products


def _job_runner() -> JobRunner:
    return JOB_RUNNER if _tenant_name() == "default" else tenants.current().jobs


def _outbox_store():
    return OUTBOX if _tenant_name() == "default" else tenants.current().outbox


def _store_call(operation: str, callback, *args, **kwargs):
    """Run a real store operation inside a bounded, safe trace span."""
    with trace_span("store", op=operation):
        return callback(*args, **kwargs)


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
        if settings.current().values["AGENT_QA_REQUIRE_IF_MATCH"]:
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
    tenant_items = tenants.list_tenants()
    tenant_orders: dict[str, int] = {}
    tenant_products: dict[str, int] = {}
    job_statuses: dict[str, int] = {}
    outbox_statuses: dict[str, int] = {}
    outbox_dropped = 0
    for item in tenant_items:
        name = str(item["tenant"])
        bundle = tenants.get(name)
        orders = ORDER_STORE if name == "default" else bundle.orders
        products = PRODUCT_STORE if name == "default" else bundle.products
        jobs = JOB_RUNNER if name == "default" else bundle.jobs
        outbox = OUTBOX if name == "default" else bundle.outbox
        tenant_orders[name] = orders.list(limit=1)[1]
        tenant_products[name] = products.list(limit=1)[1]
        for status, count in jobs.status_counts().items():
            job_statuses[status] = job_statuses.get(status, 0) + count
        statuses, dropped = outbox.metrics_snapshot()
        for status, count in statuses.items():
            outbox_statuses[status] = outbox_statuses.get(status, 0) + count
        outbox_dropped += dropped
    order_count = sum(tenant_orders.values())
    product_count = sum(tenant_products.values())
    audit_entries, audit_dropped = AUDIT_LOG.metrics_snapshot()
    return (
        200,
        REGISTRY.render(
            order_count,
            GIT_SHA,
            products=product_count,
            audit_entries=audit_entries,
            audit_dropped=audit_dropped,
            outbox_statuses=outbox_statuses,
            outbox_dropped=outbox_dropped,
            job_statuses=job_statuses,
            tenant_orders=tenant_orders,
            tenant_products=tenant_products,
            tenant_count=len(tenant_items),
        ),
        {"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
    )


def whoami(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
    *,
    identity: dict[str, object] | None = None,
) -> tuple[int, object, dict[str, str]]:
    """Return the authenticated key's public identity fields."""
    if identity is None:
        raise ApiError(401, "unauthorized", "A valid API key is required")
    return (
        200,
        {name: identity[name] for name in ("key_id", "role", "label", "tenants")},
        {},
    )


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
    body = _key_payload(payload, {"role", "label", "tenants"})
    role, label = body.get("role"), body.get("label")
    if not isinstance(role, str) or role not in {"read", "write", "admin"}:
        raise ApiError(400, "invalid_role", "Role must be read, write, or admin")
    if not isinstance(label, str) or not label.strip() or len(label) > 40:
        raise ApiError(
            400, "validation_error", "Label must be 1 to 40 non-blank characters"
        )
    return 201, auth.KEY_STORE.create(role, label, body.get("tenants")), {}


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


def versions(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return the published API version lifecycle document."""
    return 200, versions_document(), {}


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
    service = FulfillmentService(
        _order_store(), _product_store(), lock=tenants.current().fulfillment_lock
    )
    order = _store_call("create", service.create, **values, include_version=True)
    version = order.pop("version")
    _outbox_store().emit("order.created", dict(order))
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
    service = FulfillmentService(
        _order_store(), _product_store(), lock=tenants.current().fulfillment_lock
    )
    status, body = _store_call(
        "create_bulk",
        service.create_bulk,
        values.get("items"),
        values.get("atomic", False),
    )
    if isinstance(body, dict):
        for result in body.get("results", []):
            if result.get("status") == 201 and isinstance(result.get("data"), dict):
                _outbox_store().emit("order.created", dict(result["data"]))
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
    store = _order_store()
    result = _store_call("list", store.list, **filters)
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
    store = _order_store()
    order = (
        _store_call("read", store.get, order_id, include_version=True)
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
    store = _order_store()
    if _store_call("read", store.get, order_id) is None:
        raise ApiError(404, "order_not_found", "Order not found")
    expected_version = _expected_version(request_headers)
    service = FulfillmentService(
        _order_store(), _product_store(), lock=tenants.current().fulfillment_lock
    )
    order = _store_call(
        "update",
        service.update,
        order_id,
        changes,
        expected_version=expected_version,
        include_version=True,
    )
    if order is None:
        raise ApiError(404, "order_not_found", "Order not found")
    version = order.pop("version")
    _outbox_store().emit("order.updated", dict(order))
    return 200, order, {"ETag": etag_for("order", order_id, version)}


def delete_order(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
    request_headers: dict[str, str] | None = None,
) -> tuple[int, object, dict[str, str]]:
    """Delete an order and return an empty response body."""
    order_id = _order_id(path_params)
    store = _order_store()
    if order_id is None or _store_call("read", store.get, order_id) is None:
        raise ApiError(404, "order_not_found", "Order not found")
    expected_version = _expected_version(request_headers)
    service = FulfillmentService(
        _order_store(), _product_store(), lock=tenants.current().fulfillment_lock
    )
    if not _store_call(
        "delete",
        service.delete,
        order_id,
        expected_version=expected_version,
    ):
        raise ApiError(404, "order_not_found", "Order not found")
    _outbox_store().emit("order.deleted", {"id": order_id})
    return 204, None, {"Content-Length": "0"}


def _v2_order(order: dict[str, object]) -> dict[str, object]:
    order_id = order["id"]
    return {
        "id": order_id,
        "customer": {"id": order["customer_id"]},
        "amount": {"total_cents": order["total_cents"], "currency": "USD"},
        "status": order["status"],
        "items": [dict(item) for item in order.get("items", [])],
        "created_at": order["created_at"],
        "links": {"self": f"/v2/orders/{order_id}"},
    }


def _v2_body(payload: object, schema: dict[str, object]) -> dict[str, object]:
    errors = validate(schema, payload)
    if errors:
        for error in errors:
            field = error["field"]
            error["field"] = field[5:] if field.startswith("body.") else field
        raise ApiError(400, "validation_error", "Request validation failed", errors)
    return payload  # type: ignore[return-value]


def _v2_query(query: list[tuple[str, str]]) -> dict[str, object]:
    filters = validate_query_params(
        [
            {
                "name": "limit",
                "in": "query",
                "schema": {
                    "type": "integer",
                    "minimum": MIN_LIMIT,
                    "maximum": MAX_LIMIT,
                    "default": DEFAULT_LIMIT,
                },
            },
            {
                "name": "cursor",
                "in": "query",
                "schema": {
                    "type": "string",
                    "maxLength": MAX_CURSOR_LENGTH,
                },
            },
            {
                "name": "status",
                "in": "query",
                "schema": {"type": "string", "enum": list(STATUSES)},
            },
            {
                "name": "customer",
                "in": "query",
                "schema": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                },
            },
            {
                "name": "sort",
                "in": "query",
                "schema": {
                    "type": "string",
                    "enum": ["id", "-id"],
                    "default": "id",
                },
            },
        ],
        query,
    )
    if "customer" in filters and not filters["customer"].strip():
        raise ApiError(
            400,
            "invalid_query",
            "Query validation failed",
            [{"field": "customer", "message": "Must not be blank"}],
        )
    filters["pagination"] = "cursor"
    filters["customer_id"] = filters.pop("customer", None)
    return filters


def create_order_v2(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Create an order using the v2 resource representation."""
    body = _v2_body(payload, V2_CREATE_ORDER_SCHEMA)
    customer = body["customer"]
    values: dict[str, object] = {"customer_id": customer["id"]}  # type: ignore[index]
    if "items" in body:
        values["items"] = body["items"]
    else:
        values["total_cents"] = body["amount"]["total_cents"]  # type: ignore[index]
    service = FulfillmentService(
        _order_store(), _product_store(), lock=tenants.current().fulfillment_lock
    )
    order = _store_call("create", service.create, **values, include_version=True)
    version = order.pop("version")
    _outbox_store().emit("order.created", dict(order))
    result = _v2_order(order)
    return (
        201,
        result,
        {
            "Location": result["links"]["self"],  # type: ignore[index]
            "ETag": etag_for("order", order["id"], version),
        },
    )


def list_orders_v2(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
    request_headers: dict[str, str] | None = None,
) -> tuple[int, object, dict[str, str]]:
    """Return cursor-paginated orders using the v2 representation."""
    filters = _v2_query(query)
    try:
        store = _order_store()
        orders, total, next_cursor = _store_call("list", store.list, **filters)
    except OrderError as error:
        raise ApiError(400, error.code, error.message, error.details) from error
    body = {
        "data": [_v2_order(order) for order in orders],
        "page": {
            "limit": filters["limit"],
            "next_cursor": next_cursor,
            "total": total,
        },
    }
    etag = _list_etag(body)
    headers = {"ETag": etag}
    if _if_none_match(request_headers, etag):
        return 304, None, headers
    return 200, body, headers


def get_order_v2(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
    request_headers: dict[str, str] | None = None,
) -> tuple[int, object, dict[str, str]]:
    """Read an order in the v2 representation."""
    order_id = _order_id(path_params)
    store = _order_store()
    order = (
        _store_call("read", store.get, order_id, include_version=True)
        if order_id
        else None
    )
    if order is None:
        raise ApiError(404, "order_not_found", "Order not found")
    version = order.pop("version")
    etag = etag_for("order", order_id, version)
    if _if_none_match(request_headers, etag):
        return 304, None, {"ETag": etag}
    return 200, _v2_order(order), {"ETag": etag}


def patch_order_v2(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
    request_headers: dict[str, str] | None = None,
) -> tuple[int, object, dict[str, str]]:
    """Update an order with v2 fields and conditional semantics."""
    body = _v2_body(payload, V2_UPDATE_ORDER_SCHEMA)
    changes: dict[str, object] = {}
    if "status" in body:
        changes["status"] = body["status"]
    if "amount" in body:
        changes["total_cents"] = body["amount"]["total_cents"]  # type: ignore[index]
    order_id = _order_id(path_params)
    store = _order_store()
    if order_id is None or _store_call("read", store.get, order_id) is None:
        raise ApiError(404, "order_not_found", "Order not found")
    expected_version = _expected_version(request_headers)
    service = FulfillmentService(
        _order_store(), _product_store(), lock=tenants.current().fulfillment_lock
    )
    order = _store_call(
        "update",
        service.update,
        order_id,
        changes,
        expected_version=expected_version,
        include_version=True,
    )
    if order is None:
        raise ApiError(404, "order_not_found", "Order not found")
    version = order.pop("version")
    _outbox_store().emit("order.updated", dict(order))
    return 200, _v2_order(order), {"ETag": etag_for("order", order_id, version)}


def delete_order_v2(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
    request_headers: dict[str, str] | None = None,
) -> tuple[int, object, dict[str, str]]:
    """Delete an order using the shared fulfillment service."""
    order_id = _order_id(path_params)
    store = _order_store()
    if order_id is None or _store_call("read", store.get, order_id) is None:
        raise ApiError(404, "order_not_found", "Order not found")
    expected_version = _expected_version(request_headers)
    service = FulfillmentService(
        _order_store(), _product_store(), lock=tenants.current().fulfillment_lock
    )
    if not _store_call(
        "delete",
        service.delete,
        order_id,
        expected_version=expected_version,
    ):
        raise ApiError(404, "order_not_found", "Order not found")
    _outbox_store().emit("order.deleted", {"id": order_id})
    return 204, None, {"Content-Length": "0"}


def create_product(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Validate and create a product."""
    store = _product_store()
    product = _store_call(
        "create",
        store.create,
        **validate_product_create(payload),
        include_version=True,
    )
    version = product.pop("version")
    _outbox_store().emit("product.created", dict(product))
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
    store = _product_store()
    status, body = _store_call(
        "create_bulk",
        store.create_bulk,
        values.get("items"),
        values.get("atomic", False),
    )
    if isinstance(body, dict):
        for result in body.get("results", []):
            if result.get("status") == 201 and isinstance(result.get("data"), dict):
                _outbox_store().emit("product.created", dict(result["data"]))
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
    store = _product_store()
    result = _store_call("list", store.list, **filters)
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


def _csv_filters(query: list[tuple[str, str]], resource: str) -> dict[str, object]:
    """Validate only the filters supported by the CSV export endpoints."""
    allowed = {
        "products": {"category", "active", "in_stock", "q"},
        "orders": {"status", "customer_id"},
    }[resource]
    errors = []
    seen = set()
    filtered = []
    for index, pair in enumerate(query):
        if index >= 10:
            errors.append({"field": "query", "message": "Too many query parameters"})
            break
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            errors.append({"field": "query", "message": "Invalid query parameter"})
            continue
        name, value = pair
        if not isinstance(name, str) or not isinstance(value, str):
            errors.append({"field": str(name), "message": "Invalid query parameter"})
        elif len(name) > 128 or len(value) > 4096:
            errors.append(
                {"field": name[:128], "message": "Query parameter is too long"}
            )
        elif name not in allowed:
            errors.append({"field": name, "message": "Unsupported query parameter"})
        elif name in seen:
            errors.append({"field": name, "message": "Parameter may appear once"})
        else:
            seen.add(name)
            filtered.append((name, value))
    if errors:
        raise ApiError(400, "invalid_query", "Invalid query parameters", errors)
    parsed = (
        validate_product_query(filtered)
        if resource == "products"
        else validate_query(filtered)
    )
    return {name: parsed[name] for name in allowed if name in parsed}


def _csv_response(
    filename: str,
    columns: tuple[str, ...],
    rows: list[dict[str, object]],
) -> tuple[int, str, dict[str, str]]:
    text_columns = tuple(
        name
        for name in columns
        if name
        not in {
            "id",
            "price_cents",
            "stock",
            "total_cents",
            "items_count",
            "active",
        }
    )
    return (
        200,
        render_csv(columns, rows, text_columns=text_columns),
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
    """Export filtered products as a bounded CSV representation."""
    filters = _csv_filters(query, "products")
    store = _product_store()
    return _csv_response(
        "products.csv",
        PRODUCT_COLUMNS,
        _store_call("export_rows", store.export_rows, filters),
    )


def export_orders_csv(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, str, dict[str, str]]:
    """Export filtered orders as a bounded CSV representation."""
    filters = _csv_filters(query, "orders")
    store = _order_store()
    return _csv_response(
        "orders.csv",
        ORDER_COLUMNS,
        _store_call("export_rows", store.export_rows, filters),
    )


def _csv_import_options(query: list[tuple[str, str]]) -> tuple[str, str]:
    values = {}
    errors = []
    for index, pair in enumerate(query):
        if index >= 10:
            errors.append({"field": "query", "message": "Too many query parameters"})
            break
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            errors.append({"field": "query", "message": "Invalid query parameter"})
            continue
        name, value = pair
        if not isinstance(name, str) or not isinstance(value, str):
            errors.append({"field": str(name), "message": "Invalid query parameter"})
        elif len(name) > 128 or len(value) > 16:
            errors.append(
                {"field": name[:128], "message": "Query parameter is too long"}
            )
        elif name not in {"mode", "on_error"}:
            errors.append({"field": name, "message": "Unsupported query parameter"})
        elif name in values:
            errors.append({"field": name, "message": "Parameter may appear once"})
        else:
            values[name] = value
    mode = values.get("mode", "apply")
    on_error = values.get("on_error", "abort")
    if mode not in {"apply", "validate"}:
        errors.append({"field": "mode", "message": "Must be apply or validate"})
    if on_error not in {"abort", "skip"}:
        errors.append({"field": "on_error", "message": "Must be abort or skip"})
    if errors:
        raise ApiError(400, "invalid_query", "Invalid query parameters", errors)
    return mode, on_error


def _import_csv(
    resource: str, query: list[tuple[str, str]], payload: object
) -> tuple[int, dict[str, object], dict[str, str]]:
    if not isinstance(payload, str):
        raise ApiError(
            400,
            "invalid_csv",
            "Request body must be CSV",
            [{"field": "body", "message": "Must be CSV text"}],
        )
    mode, on_error = _csv_import_options(query)
    rows = parse_csv(payload, resource)
    store = _product_store() if resource == "products" else _order_store()
    status, report = store.import_rows(rows, mode=mode, on_error=on_error)
    context = get_context()
    if context is not None:
        context.resource = resource
        context.changes = {
            "summary": {
                "created": report.get("created", 0),
                "failed": report.get("failed", 0),
            }
        }
    return status, report, {}


def import_products_csv(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, dict[str, object], dict[str, str]]:
    """Validate or atomically import products from CSV."""
    return _import_csv("products", query, payload)


def import_orders_csv(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, dict[str, object], dict[str, str]]:
    """Validate or atomically import orders from CSV."""
    return _import_csv("orders", query, payload)


_SEARCH_SORTS = {"orders": ("id", "-id"), "products": PRODUCT_SORTS}
_SEARCH_QUERY_PARAMETERS = frozenset(("q", "sort", "limit", "offset"))
_EXPLAIN_QUERY_PARAMETERS = frozenset(("resource", "q"))


def _search_parameters(resource: str) -> list[dict[str, object]]:
    sort_values = list(_SEARCH_SORTS[resource])
    return [
        {
            "name": "q",
            "in": "query",
            "required": True,
            "description": _search_expression_description(resource),
            "schema": {"type": "string", "maxLength": 500},
        },
        {
            "name": "sort",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "enum": sort_values, "default": "id"},
        },
        {
            "name": "limit",
            "in": "query",
            "required": False,
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
            "schema": {
                "type": "integer",
                "minimum": MIN_OFFSET,
                "maximum": (1 << 63) - 1,
                "default": DEFAULT_OFFSET,
            },
        },
    ]


def _search_field_descriptions(resource: str) -> list[str]:
    field_descriptions = []
    for field, metadata in FIELD_METADATA[resource].items():
        description = f"{field} ({metadata['type']})"
        if metadata.get("multi"):
            description += "; matches any value"
        if "enum" in metadata:
            description += f" values {', '.join(metadata['enum'])}"
        operators = (
            operator.upper() if operator == "in" else operator
            for operator in supported_operators(metadata)
        )
        description += "; operators " + ", ".join(operators)
        field_descriptions.append(description)
    return field_descriptions


def _search_expression_description(resource: str) -> str:
    fields = "; ".join(_search_field_descriptions(resource))
    return (
        f"Search expression for {resource}, at most 500 characters. Fields: {fields}."
    )


def _search_response_schema(resource: str) -> dict[str, object]:
    schema = deepcopy(SCHEMAS["SearchResult"])
    item_name = "Order" if resource == "orders" else "Product"
    schema["properties"]["items"]["items"] = {
        "$ref": f"#/components/schemas/{item_name}"
    }
    return schema


def _search_explain_schema() -> dict[str, object]:
    schema = deepcopy(SCHEMAS["SearchExplain"])
    schema["properties"]["resource"]["enum"] = list(FIELD_METADATA)
    return schema


def _search_query(query: object, allowed: frozenset[str]) -> dict[str, str]:
    """Validate the small, bounded query string accepted by search routes."""
    if not isinstance(query, (list, tuple)):
        message = "Invalid query parameters"
        raise ApiError(
            400, "invalid_query", message, [{"field": "q", "message": message}]
        )
    if len(query) > len(allowed):
        message = "Too many query parameters"
        raise ApiError(
            400, "invalid_query", message, [{"field": "q", "message": message}]
        )
    values: dict[str, str] = {}
    for pair in query:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            message = "Invalid query parameter"
            raise ApiError(
                400, "invalid_query", message, [{"field": "q", "message": message}]
            )
        name, value = pair
        if not isinstance(name, str) or not isinstance(value, str):
            message = "Invalid query parameter"
            raise ApiError(
                400, "invalid_query", message, [{"field": "q", "message": message}]
            )
        if len(name) > 20 or (name != "q" and len(value) > 500):
            message = "Invalid query parameter"
            raise ApiError(
                400, "invalid_query", message, [{"field": "q", "message": message}]
            )
        if name not in allowed:
            message = f"Unsupported query parameter: {name}"
            raise ApiError(
                400, "invalid_query", message, [{"field": "q", "message": message}]
            )
        if name in values:
            message = f"The {name} parameter may appear once"
            raise ApiError(
                400, "invalid_query", message, [{"field": "q", "message": message}]
            )
        values[name] = value
    if "q" not in values:
        message = "The q parameter is required"
        raise ApiError(
            400, "invalid_query", message, [{"field": "q", "message": message}]
        )
    return values


def _search_page(values: dict[str, str], resource: str) -> tuple[str, int, int]:
    query = [
        (name, values[name]) for name in ("sort", "limit", "offset") if name in values
    ]
    if resource == "orders":
        try:
            filters = validate_query(query)
        except OrderError as error:
            raise ApiError(400, error.code, error.message, error.details) from error
    else:
        filters = validate_product_query(query)
    return filters["sort"], filters["limit"], filters["offset"]


def _search(
    query: list[tuple[str, str]], resource: str
) -> tuple[int, object, dict[str, str]]:
    values = _search_query(query, _SEARCH_QUERY_PARAMETERS)
    ast = parse(values["q"], resource)
    sort, limit, offset = _search_page(values, resource)
    store = _order_store() if resource == "orders" else _product_store()
    items, total = store.search(
        compile_predicate(ast, resource), sort=sort, limit=limit, offset=offset
    )
    return (
        200,
        {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "query": {"normalized": normalize(ast), "terms": count_terms(ast)},
        },
        {},
    )


def search_orders(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Search orders using the public query DSL."""
    return _search(query, "orders")


def search_products(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Search products using the public query DSL."""
    return _search(query, "products")


def search_explain(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return the deterministic parsed form of a search query."""
    values = _search_query(query, _EXPLAIN_QUERY_PARAMETERS)
    resource = values.get("resource", "")
    if resource not in ("orders", "products"):
        message = "resource must be orders or products"
        raise ApiError(
            400, "invalid_query", message, [{"field": "q", "message": message}]
        )
    ast = parse(values["q"], resource)
    return 200, {"resource": resource, "normalized": normalize(ast), "ast": ast}, {}


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
    store = _product_store()
    product = (
        _store_call(
            "read",
            store.get,
            product_id,
            include_version=True,
        )
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
    store = _product_store()
    if _store_call("read", store.get, product_id) is None:
        raise ApiError(404, "product_not_found", "Product not found")
    expected_version = _expected_version(request_headers)
    product = _store_call(
        "update",
        store.update,
        product_id,
        changes,
        expected_version=expected_version,
        include_version=True,
    )
    if product is None:
        raise ApiError(404, "product_not_found", "Product not found")
    version = product.pop("version")
    _outbox_store().emit("product.updated", dict(product))
    return 200, product, {"ETag": etag_for("product", product_id, version)}


def delete_product(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
    request_headers: dict[str, str] | None = None,
) -> tuple[int, object, dict[str, str]]:
    """Delete a product and return an empty response body."""
    product_id = _product_id(path_params)
    store = _product_store()
    if product_id is None or _store_call("read", store.get, product_id) is None:
        raise ApiError(404, "product_not_found", "Product not found")
    expected_version = _expected_version(request_headers)
    if not _store_call(
        "delete", store.delete, product_id, expected_version=expected_version
    ):
        raise ApiError(404, "product_not_found", "Product not found")
    _outbox_store().emit("product.deleted", {"id": product_id})
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
    store = _product_store()
    if _store_call("read", store.get, product_id) is None:
        raise ApiError(404, "product_not_found", "Product not found")
    expected_version = _expected_version(request_headers)
    product = _store_call(
        "adjust_stock",
        store.adjust_stock,
        product_id,
        values["delta"],
        expected_version=expected_version,
        include_version=True,
    )
    if product is None:
        raise ApiError(404, "product_not_found", "Product not found")
    version = product.pop("version")
    _outbox_store().emit("product.updated", dict(product))
    return 200, product, {"ETag": etag_for("product", product_id, version)}


def categories(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return current aggregates for categories represented by products."""
    store = _product_store()
    items = _store_call("categories", store.categories)
    return 200, {"items": items, "total": len(items)}, {}


def list_audit(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return bounded, filtered audit entries for administrators."""
    filters = validate_query_params(_AUDIT_QUERY_PARAMETERS, query)
    requested_tenant = filters.pop("tenant", _tenant_name())
    if requested_tenant != "*":
        requested_tenant = tenants.validate_name(requested_tenant)
    filters["tenant"] = requested_tenant
    return 200, AUDIT_LOG.query(**filters), {}


def _trace_filters(query: list[tuple[str, str]]) -> dict[str, object]:
    allowed = {"route", "status", "min_duration_ms", "limit"}
    values: dict[str, str] = {}
    if len(query) > len(allowed):
        raise ApiError(400, "invalid_query", "Invalid trace query")
    for key, value in query:
        if key not in allowed or key in values or len(value) > 128:
            raise ApiError(400, "invalid_query", "Invalid trace query")
        values[key] = value
    filters: dict[str, object] = {"limit": 20}
    if "route" in values:
        route = values["route"]
        if route != "unmatched" and not re.fullmatch(
            r"/[A-Za-z0-9_./{}-]{0,127}", route
        ):
            raise ApiError(400, "invalid_query", "Invalid trace query")
        filters["route"] = route
    if "status" in values:
        raw = values["status"]
        if not raw.isascii() or not re.fullmatch(r"[+-]?[0-9]+", raw):
            raise ApiError(400, "invalid_query", "Invalid trace query")
        try:
            status_code = int(raw)
        except ValueError as error:
            raise ApiError(400, "invalid_query", "Invalid trace query") from error
        filters["status"] = status_code
    if "min_duration_ms" in values:
        raw = values["min_duration_ms"]
        if len(raw) > 32 or not re.fullmatch(
            r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", raw
        ):
            raise ApiError(400, "invalid_query", "Invalid trace query")
        try:
            minimum = float(raw)
        except ValueError as error:
            raise ApiError(400, "invalid_query", "Invalid trace query") from error
        if not math.isfinite(minimum) or minimum < 0:
            raise ApiError(400, "invalid_query", "Invalid trace query")
        filters["min_duration_ms"] = minimum
    if "limit" in values:
        raw = values["limit"]
        if not raw.isascii() or not raw.isdigit() or len(raw) > 3:
            raise ApiError(400, "invalid_query", "Invalid trace query")
        limit = int(raw)
        if not 1 <= limit <= 100:
            raise ApiError(400, "invalid_query", "Invalid trace query")
        filters["limit"] = limit
    return filters


def list_traces(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """List retained traces using bounded filters."""
    from agent_qa.tracing import TRACE_BUFFER

    return 200, TRACE_BUFFER.list(**_trace_filters(query)), {}


def get_trace(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return one retained trace."""
    from agent_qa.tracing import TRACE_BUFFER

    trace_id = (path_params or {}).get("trace_id", "")
    if not re.fullmatch(r"[0-9a-f]{32}", trace_id):
        raise ApiError(404, "trace_not_found", "Trace not found")
    record = TRACE_BUFFER.get(trace_id)
    if record is None:
        raise ApiError(404, "trace_not_found", "Trace not found")
    return 200, record, {}


def get_audit_entry(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return one retained audit entry by its sequence number."""
    raw_seq = (path_params or {}).get("seq", "")
    requested = validate_query_params(
        [
            {
                "name": "tenant",
                "in": "query",
                "schema": {
                    "type": "string",
                    "maxLength": 24,
                    "pattern": r"^(\*|[a-z0-9][a-z0-9-]{0,23})$",
                },
            }
        ],
        query,
    ).get("tenant", _tenant_name())
    if requested != "*":
        requested = tenants.validate_name(requested)
    if len(raw_seq) > 20 or not raw_seq.isascii() or not raw_seq.isdigit():
        entry = None
    else:
        try:
            seq = int(raw_seq)
        except ValueError:
            seq = 0
        entry = AUDIT_LOG.get(seq, tenant=requested) if seq > 0 else None
    if entry is not None and requested != "*":
        if entry.get("tenant", "default") != requested:
            entry = None
    if entry is None:
        raise ApiError(404, "audit_entry_not_found", "Audit entry not found")
    return 200, entry, {}


def list_tenants(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    items = [
        {
            key: item[key]
            for key in (
                "tenant",
                "orders",
                "products",
                "webhooks",
                "jobs",
                "created_at",
            )
        }
        for item in tenants.list_tenants()
    ]
    return 200, {"items": items, "total": len(items)}, {}


def delete_tenant(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    tenants.delete((path_params or {}).get("tenant", ""))
    return 204, None, {"Content-Length": "0"}


def _rate_limit_identity(path_params: dict[str, str] | None) -> str:
    identity = (path_params or {}).get("identity", "")
    if (
        len(identity) > 71
        or re.fullmatch(r"(?:key|client|ip):[A-Za-z0-9._:-]{1,64}", identity) is None
    ):
        raise ApiError(
            400,
            "validation_error",
            "Invalid request path",
            [
                {
                    "field": "identity",
                    "message": "Must be a valid rate limit identity",
                }
            ],
        )
    return identity


def list_rate_limits(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return the default rate limit policy and current overrides."""
    return 200, RATE_LIMITER.snapshot(), {}


def put_rate_limit(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Set an identity-specific rate limit policy and reset its bucket."""
    identity = _rate_limit_identity(path_params)
    errors = validate(SCHEMAS["RateLimitOverrideRequest"], payload)
    if errors:
        raise ApiError(400, "validation_error", "Request validation failed", errors)
    assert isinstance(payload, dict)
    burst = payload["burst"]
    refill_per_second = payload["refill_per_second"]
    RATE_LIMITER.set_override(identity, burst, refill_per_second)
    return (
        200,
        {
            "identity": identity,
            "burst": burst,
            "refill_per_second": refill_per_second,
        },
        {},
    )


def delete_rate_limit(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Remove an identity-specific rate limit policy and reset its bucket."""
    identity = _rate_limit_identity(path_params)
    if not RATE_LIMITER.delete_override(identity):
        raise ApiError(404, "override_not_found", "Rate limit override not found")
    return 204, None, {}


def list_config(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return the registered settings with secret values redacted."""
    loaded = settings.current()
    return (
        200,
        {
            "settings": {
                name: settings.describe(loaded, name)
                for name in sorted(settings.SETTINGS)
            },
            "unknown_env": list(loaded.unknown_env),
            "valid": loaded.valid,
        },
        {},
    )


def get_config_setting(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return metadata for one registered setting."""
    name = (path_params or {}).get("name", "")
    if len(name) > 128 or name not in settings.SETTINGS:
        raise ApiError(404, "setting_not_found", "Setting not found")
    loaded = settings.current()
    return 200, {"name": name, **settings.describe(loaded, name)}, {}


_CONFIG_SCALAR_SCHEMA = {
    "oneOf": [
        {"type": "string"},
        {"type": "number"},
        {"type": "boolean"},
    ]
}
_CONFIG_SETTING_SCHEMA = {
    "type": "object",
    "required": [
        "value",
        "default",
        "source",
        "type",
        "secret",
        "description",
        "is_default",
    ],
    "properties": {
        "value": _CONFIG_SCALAR_SCHEMA,
        "default": _CONFIG_SCALAR_SCHEMA,
        "source": {"type": "string", "enum": ["env", "default"]},
        "type": {"type": "string", "enum": ["int", "float", "bool", "str"]},
        "secret": {"type": "boolean"},
        "description": {"type": "string"},
        "is_default": {"type": "boolean"},
    },
    "additionalProperties": False,
}
_CONFIG_SETTING_DETAIL_SCHEMA = {
    **_CONFIG_SETTING_SCHEMA,
    "required": ["name", *_CONFIG_SETTING_SCHEMA["required"]],
    "properties": {
        "name": {"type": "string"},
        **_CONFIG_SETTING_SCHEMA["properties"],
    },
}
_CONFIG_ERROR_SCHEMA = {
    "type": "object",
    "required": ["field", "message"],
    "properties": {"field": {"type": "string"}, "message": {"type": "string"}},
    "additionalProperties": False,
}


def validate_config(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Dry-run validation of a bounded set of environment setting values."""
    if not isinstance(payload, dict) or set(payload) != {"env"}:
        raise ApiError(400, "validation_error", "Invalid request body")
    env = payload["env"]
    if not isinstance(env, dict):
        raise ApiError(
            400,
            "validation_error",
            "Request validation failed",
            [{"field": "env", "message": "Must be an object"}],
        )
    if len(env) > 50:
        raise ApiError(
            400,
            "validation_error",
            "Request validation failed",
            [{"field": "env", "message": "Must contain at most 50 settings"}],
        )
    for name, value in env.items():
        if not isinstance(name, str):
            raise ApiError(
                400,
                "validation_error",
                "Request validation failed",
                [{"field": "env", "message": "Setting names must be strings"}],
            )
        if not isinstance(value, str):
            raise ApiError(
                400,
                "validation_error",
                "Request validation failed",
                [{"field": name, "message": "Must be a string"}],
            )
    loaded = settings.load(env)
    return (
        200,
        {
            "valid": loaded.valid,
            "errors": [
                {"field": error.field, "message": error.message}
                for error in sorted(loaded.errors, key=lambda error: error.field)
            ],
            "unknown": sorted(set(env) - set(settings.SETTINGS)),
        },
        {},
    )


def create_webhook(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    return 201, _outbox_store().create_webhook(payload), {}


def list_webhooks(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    return 200, _outbox_store().list_webhooks(), {}


def get_webhook(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    return 200, _outbox_store().get_webhook((path_params or {}).get("id", "")), {}


def patch_webhook(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    return (
        200,
        _outbox_store().patch_webhook((path_params or {}).get("id", ""), payload),
        {},
    )


def delete_webhook(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    _outbox_store().delete_webhook((path_params or {}).get("id", ""))
    return 204, None, {"Content-Length": "0"}


def list_outbox(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    filters = validate_query_params(_OUTBOX_QUERY_PARAMETERS, query)
    normalized = [(name, str(value)) for name, value in filters.items()]
    return 200, _outbox_store().list_outbox(normalized), {}


def get_outbox(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    return 200, _outbox_store().get_outbox((path_params or {}).get("id", "")), {}


def process_outbox(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    values = payload if isinstance(payload, dict) else {}
    return (
        200,
        _outbox_store().process_due(
            ignore_schedule=values.get("ignore_schedule", False),
            max_items=values.get("max", 50),
        ),
        {},
    )


def requeue_outbox(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    return 200, _outbox_store().requeue((path_params or {}).get("id", "")), {}


def get_dispatcher(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    return 200, _outbox_store().get_dispatcher(), {}


def put_dispatcher(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    return 200, _outbox_store().configure_dispatcher(payload), {}


def create_job(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Queue a validated background job."""
    values = payload if isinstance(payload, dict) else {}
    job = _job_runner().submit(values["type"], values.get("params"))
    return 202, job, {"Location": f"/jobs/{job['id']}"}


def list_jobs(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return filtered, paginated jobs in creation order."""
    filters = validate_query_params(_JOB_LIST_QUERY_PARAMETERS, query)
    return 200, _job_runner().list_jobs(**filters), {}


def _job_id(path_params: dict[str, str] | None) -> int | None:
    raw_id = (path_params or {}).get("id", "")
    if len(raw_id) > 19 or not raw_id.isascii() or not raw_id.isdigit():
        return None
    try:
        job_id = int(raw_id)
    except ValueError:
        return None
    return job_id if job_id > 0 else None


def get_job(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Return a job, optionally waiting on its condition for completion."""
    filters = validate_query_params(_JOB_WAIT_QUERY_PARAMETERS, query)
    job_id = _job_id(path_params)
    if job_id is None:
        raise ApiError(404, "job_not_found", "Job not found")
    job = _job_runner().get(job_id, wait_ms=filters.get("wait_ms", 0))
    if job is None:
        raise ApiError(404, "job_not_found", "Job not found")
    return 200, job, {}


def cancel_job(
    query: list[tuple[str, str]],
    path_params: dict[str, str] | None = None,
    payload: object = None,
) -> tuple[int, object, dict[str, str]]:
    """Request cooperative cancellation of a queued or running job."""
    job_id = _job_id(path_params)
    if job_id is None:
        raise ApiError(404, "job_not_found", "Job not found")
    job = _job_runner().cancel(job_id)
    if job is None:
        raise ApiError(404, "job_not_found", "Job not found")
    return 200, job, {}


_JOB_LIST_QUERY_PARAMETERS = [
    {
        "name": "status",
        "in": "query",
        "schema": {"type": "string", "enum": list(JOB_STATUSES)},
    },
    {
        "name": "type",
        "in": "query",
        "schema": {"type": "string", "enum": list(JOB_TYPES)},
    },
    {
        "name": "limit",
        "in": "query",
        "schema": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "default": 20,
        },
    },
    {
        "name": "offset",
        "in": "query",
        "schema": {
            "type": "integer",
            "minimum": 0,
            "maximum": (1 << 63) - 1,
            "default": 0,
        },
    },
]
_JOB_WAIT_QUERY_PARAMETERS = [
    {
        "name": "wait_ms",
        "in": "query",
        "schema": {
            "type": "integer",
            "minimum": 0,
            "maximum": 5000,
            "default": 0,
        },
    }
]
_OUTBOX_ID = {
    "name": "id",
    "in": "path",
    "required": True,
    "schema": {"type": "integer", "minimum": 1, "maximum": (1 << 31) - 1},
}
_OUTBOX_QUERY_PARAMETERS = [
    {
        "name": "status",
        "in": "query",
        "schema": {
            "type": "string",
            "enum": ["pending", "retrying", "delivered", "failed"],
        },
    },
    {
        "name": "webhook_id",
        "in": "query",
        "schema": {"type": "integer", "minimum": 1, "maximum": (1 << 31) - 1},
    },
    {
        "name": "event_type",
        "in": "query",
        "schema": {
            "type": "string",
            "enum": [
                "order.created",
                "order.updated",
                "order.deleted",
                "product.created",
                "product.updated",
                "product.deleted",
            ],
        },
    },
    {
        "name": "limit",
        "in": "query",
        "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
    },
    {
        "name": "offset",
        "in": "query",
        "schema": {
            "type": "integer",
            "minimum": 0,
            "maximum": (1 << 31) - 1,
            "default": 0,
        },
    },
]


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
        "name": "tenant",
        "in": "query",
        "schema": {
            "type": "string",
            "maxLength": 24,
            "pattern": r"^(\*|[a-z0-9][a-z0-9-]{0,23})$",
        },
        "description": "Filter by tenant; '*' includes all tenants.",
    },
    {
        "name": "method",
        "in": "query",
        "schema": {"type": "string", "enum": ["POST", "PUT", "PATCH", "DELETE"]},
    },
    {
        "name": "resource",
        "in": "query",
        "schema": {
            "type": "string",
            "enum": ["orders", "products", "keys", "jobs"],
        },
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
        "tenant",
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
            "enum": ["orders", "products", "keys", "jobs"],
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
        "tenant": {
            "type": "string",
            "pattern": "^[a-z0-9][a-z0-9-]{0,23}$",
        },
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
    "required": ["key_id", "role", "label", "key", "created_at", "tenants"],
    "properties": {
        "key_id": {"type": "string"},
        "role": {"type": "string", "enum": ["read", "write", "admin"]},
        "label": {"type": "string"},
        "key": {"type": "string"},
        "created_at": {"type": "string", "format": "date-time"},
        "tenants": {"type": "array", "items": {"type": "string"}, "nullable": True},
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
                    "tenants",
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
                    "tenants": {
                        "type": "array",
                        "items": {"type": "string"},
                        "nullable": True,
                    },
                },
                "additionalProperties": False,
            },
        },
        "total": {"type": "integer", "minimum": 1, "maximum": 21},
    },
    "additionalProperties": False,
}

_CSV_IMPORT_REPORT_SCHEMA = {
    "type": "object",
    "required": ["mode", "rows", "created", "failed", "applied", "errors"],
    "properties": {
        "mode": {"type": "string", "enum": ["apply", "validate"]},
        "rows": {"type": "integer", "minimum": 0, "maximum": 200},
        "created": {"type": "integer", "minimum": 0, "maximum": 200},
        "failed": {"type": "integer", "minimum": 0, "maximum": 200},
        "applied": {"type": "boolean"},
        "errors": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["line", "field", "message"],
                "properties": {
                    "line": {"type": "integer", "minimum": 2, "maximum": 201},
                    "field": {"type": "string"},
                    "message": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}

_CSV_ROUTES = (
    {
        "method": "GET",
        "path": "/exports/products.csv",
        "handler": export_products_csv,
        "role": None,
        "auth_required": False,
        "operation_id": "exportProductsCsv",
        "summary": "Export filtered products as CSV",
        "produces": ["text/csv"],
        "parameters": [
            {
                "name": "category",
                "in": "query",
                "required": False,
                "schema": {"type": "string"},
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
                "name": "q",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "maxLength": MAX_PRODUCT_QUERY_LENGTH},
            },
        ],
        "responses": ["200", "400", "403"],
        "error_responses": {
            "400": "Invalid or unsupported export filter (invalid_query)."
        },
    },
    {
        "method": "GET",
        "path": "/exports/orders.csv",
        "handler": export_orders_csv,
        "role": None,
        "auth_required": False,
        "operation_id": "exportOrdersCsv",
        "summary": "Export filtered orders as CSV",
        "produces": ["text/csv"],
        "parameters": [
            {
                "name": "status",
                "in": "query",
                "required": False,
                "schema": _STATUS_SCHEMA,
            },
            {
                "name": "customer_id",
                "in": "query",
                "required": False,
                "schema": {"type": "string"},
            },
        ],
        "responses": ["200", "400", "403"],
        "error_responses": {
            "400": "Invalid or unsupported export filter (invalid_query)."
        },
    },
    {
        "method": "POST",
        "path": "/imports/products",
        "handler": import_products_csv,
        "body": True,
        "consumes": ["text/csv"],
        "max_body_bytes": 65536,
        "role": "write",
        "auth_required": True,
        "operation_id": "importProductsCsv",
        "summary": "Validate or import products from CSV",
        "parameters": [
            {
                "name": "mode",
                "in": "query",
                "required": False,
                "schema": {
                    "type": "string",
                    "enum": ["apply", "validate"],
                    "default": "apply",
                },
            },
            {
                "name": "on_error",
                "in": "query",
                "required": False,
                "schema": {
                    "type": "string",
                    "enum": ["abort", "skip"],
                    "default": "abort",
                },
            },
        ],
        "responses": ["200", "201", "400", "401", "403", "413", "415", "422"],
        "response_schemas": {
            "200": _CSV_IMPORT_REPORT_SCHEMA,
            "201": _CSV_IMPORT_REPORT_SCHEMA,
            "422": _CSV_IMPORT_REPORT_SCHEMA,
        },
    },
    {
        "method": "POST",
        "path": "/imports/orders",
        "handler": import_orders_csv,
        "body": True,
        "consumes": ["text/csv"],
        "max_body_bytes": 65536,
        "role": "write",
        "auth_required": True,
        "operation_id": "importOrdersCsv",
        "summary": "Validate or import orders from CSV",
        "parameters": [
            {
                "name": "mode",
                "in": "query",
                "required": False,
                "schema": {
                    "type": "string",
                    "enum": ["apply", "validate"],
                    "default": "apply",
                },
            },
            {
                "name": "on_error",
                "in": "query",
                "required": False,
                "schema": {
                    "type": "string",
                    "enum": ["abort", "skip"],
                    "default": "abort",
                },
            },
        ],
        "responses": ["200", "201", "400", "401", "403", "413", "415", "422"],
        "response_schemas": {
            "200": _CSV_IMPORT_REPORT_SCHEMA,
            "201": _CSV_IMPORT_REPORT_SCHEMA,
            "422": _CSV_IMPORT_REPORT_SCHEMA,
        },
    },
)

ROUTES = (
    {
        "method": "GET",
        "path": "/health",
        "handler": health,
        "role": None,
        "auth_required": False,
        "operation_id": "getHealth",
        "summary": "Check service health",
        "rate_limited": False,
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
        "rate_limited": False,
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
        "rate_limited": False,
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
        "rate_limited": False,
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
        "produces": ["text/plain"],
        "rate_limited": False,
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
        "path": "/versions",
        "handler": versions,
        "role": None,
        "auth_required": False,
        "operation_id": "getApiVersions",
        "summary": "Read API version lifecycle information",
        "responses": ["200", "403"],
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
        "path": "/search/explain",
        "handler": search_explain,
        "role": None,
        "auth_required": False,
        "operation_id": "explainSearch",
        "summary": "Parse and normalize a search query",
        "parameters": [
            {
                "name": "resource",
                "in": "query",
                "required": True,
                "schema": {"type": "string", "enum": list(FIELD_METADATA)},
            },
            {
                "name": "q",
                "in": "query",
                "required": True,
                "description": (
                    "Search expression, at most 500 characters. "
                    + " ".join(
                        _search_expression_description(resource)
                        for resource in FIELD_METADATA
                    )
                ),
                "schema": {"type": "string", "maxLength": 500},
            },
        ],
        "responses": ["200", "400", "403"],
        "response_schemas": {"200": _search_explain_schema()},
        "error_responses": {
            "400": (
                "Invalid parameters (invalid_query) or search syntax "
                "(invalid_search_query, with details.position)."
            )
        },
    },
    {
        "method": "GET",
        "path": "/orders",
        "handler": list_orders,
        "api_version": "1",
        "deprecated": True,
        "successor": "/v2/orders",
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
        "method": "GET",
        "path": "/orders/search",
        "handler": search_orders,
        "api_version": "1",
        "deprecated": True,
        "successor": "/v2/orders",
        "role": None,
        "auth_required": False,
        "operation_id": "searchOrders",
        "summary": "Search orders using the query DSL",
        "parameters": _search_parameters("orders"),
        "responses": ["200", "400", "403"],
        "response_schemas": {"200": _search_response_schema("orders")},
        "error_responses": {
            "400": (
                "Invalid parameters (invalid_query) or search syntax "
                "(invalid_search_query, with details.position)."
            )
        },
    },
    {
        "method": "POST",
        "path": "/orders",
        "handler": create_order,
        "api_version": "1",
        "deprecated": True,
        "successor": "/v2/orders",
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
        "api_version": "1",
        "deprecated": True,
        "successor": "/v2/orders",
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
        "api_version": "1",
        "deprecated": True,
        "successor": "/v2/orders/{id}",
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
        "api_version": "1",
        "deprecated": True,
        "successor": "/v2/orders/{id}",
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
        "api_version": "1",
        "deprecated": True,
        "successor": "/v2/orders/{id}",
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
        "method": "GET",
        "path": "/v2/orders",
        "handler": list_orders_v2,
        "api_version": "2",
        "role": None,
        "auth_required": False,
        "operation_id": "listOrdersV2",
        "summary": "List orders using the v2 representation",
        "parameters": [
            _IF_NONE_MATCH,
            {
                "name": "limit",
                "in": "query",
                "schema": {
                    "type": "integer",
                    "minimum": MIN_LIMIT,
                    "maximum": MAX_LIMIT,
                    "default": DEFAULT_LIMIT,
                },
            },
            {
                "name": "cursor",
                "in": "query",
                "schema": {
                    "type": "string",
                    "maxLength": MAX_CURSOR_LENGTH,
                },
            },
            {
                "name": "status",
                "in": "query",
                "schema": _STATUS_SCHEMA,
            },
            {
                "name": "customer",
                "in": "query",
                "schema": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                },
            },
            {
                "name": "sort",
                "in": "query",
                "schema": {
                    "type": "string",
                    "enum": ["id", "-id"],
                    "default": "id",
                },
            },
        ],
        "responses": ["200", "304", "400", "403"],
        "response_schemas": {"200": V2_ORDER_LIST_SCHEMA},
        "conditional_headers": ["If-None-Match"],
    },
    {
        "method": "POST",
        "path": "/v2/orders",
        "handler": create_order_v2,
        "api_version": "2",
        "body": True,
        "role": "write",
        "auth_required": True,
        "operation_id": "createOrderV2",
        "summary": "Create an order using the v2 representation",
        "idempotent": True,
        "etag_response": True,
        "request_schema": V2_CREATE_ORDER_SCHEMA,
        "responses": [
            "201",
            "400",
            "401",
            "409",
            "411",
            "413",
            "415",
            "422",
            "403",
        ],
        "response_schemas": {"201": V2_ORDER_SCHEMA},
    },
    {
        "method": "GET",
        "path": "/v2/orders/{id}",
        "handler": get_order_v2,
        "api_version": "2",
        "role": None,
        "auth_required": False,
        "operation_id": "getOrderV2",
        "summary": "Read an order using the v2 representation",
        "parameters": [_ORDER_ID, _IF_NONE_MATCH],
        "responses": ["200", "304", "400", "404", "403"],
        "response_schemas": {"200": V2_ORDER_SCHEMA},
        "conditional_headers": ["If-None-Match"],
    },
    {
        "method": "PATCH",
        "path": "/v2/orders/{id}",
        "handler": patch_order_v2,
        "api_version": "2",
        "body": True,
        "role": "write",
        "auth_required": True,
        "operation_id": "updateOrderV2",
        "summary": "Update an order using the v2 representation",
        "parameters": [_ORDER_ID, _IF_MATCH],
        "request_schema": V2_UPDATE_ORDER_SCHEMA,
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
        "response_schemas": {"200": V2_ORDER_SCHEMA},
        "conditional_headers": ["If-Match"],
    },
    {
        "method": "DELETE",
        "path": "/v2/orders/{id}",
        "handler": delete_order_v2,
        "api_version": "2",
        "role": "write",
        "auth_required": True,
        "operation_id": "deleteOrderV2",
        "summary": "Delete an order using the v2 representation",
        "parameters": [_ORDER_ID, _IF_MATCH],
        "responses": ["204", "400", "401", "404", "412", "428", "403"],
        "conditional_headers": ["If-Match"],
    },
    {
        "method": "GET",
        "path": "/products/search",
        "handler": search_products,
        "role": None,
        "auth_required": False,
        "operation_id": "searchProducts",
        "summary": "Search products using the query DSL",
        "parameters": _search_parameters("products"),
        "responses": ["200", "400", "403"],
        "response_schemas": {"200": _search_response_schema("products")},
        "error_responses": {
            "400": (
                "Invalid parameters (invalid_query) or search syntax "
                "(invalid_search_query, with details.position)."
            )
        },
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
                "required": ["key_id", "role", "label", "tenants"],
                "properties": {
                    "key_id": {"type": "string"},
                    "role": {"type": "string", "enum": ["read", "write", "admin"]},
                    "label": {"type": "string"},
                    "tenants": {
                        "type": "array",
                        "items": {"type": "string"},
                        "nullable": True,
                    },
                },
                "additionalProperties": False,
            }
        },
    },
    {
        "method": "GET",
        "path": "/admin/config",
        "handler": list_config,
        "role": "admin",
        "auth_required": True,
        "rate_limited": False,
        "operation_id": "listConfig",
        "summary": "Read registered configuration metadata",
        "responses": ["200", "401", "403"],
        "response_schemas": {
            "200": {
                "type": "object",
                "required": ["settings", "unknown_env", "valid"],
                "properties": {
                    "settings": {
                        "type": "object",
                        "additionalProperties": _CONFIG_SETTING_SCHEMA,
                    },
                    "unknown_env": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "valid": {"type": "boolean"},
                },
                "additionalProperties": False,
            }
        },
    },
    {
        "method": "GET",
        "path": "/admin/config/{name}",
        "handler": get_config_setting,
        "role": "admin",
        "auth_required": True,
        "rate_limited": False,
        "operation_id": "getConfigSetting",
        "summary": "Read metadata for one registered setting",
        "parameters": [
            {
                "name": "name",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z_][A-Za-z0-9_]{0,127}$",
                },
            }
        ],
        "responses": ["200", "401", "403", "404"],
        "response_schemas": {"200": _CONFIG_SETTING_DETAIL_SCHEMA},
        "error_responses": {
            "404": "The setting name is not registered (setting_not_found)."
        },
    },
    {
        "method": "POST",
        "path": "/admin/config/validate",
        "handler": validate_config,
        "body": True,
        "role": "admin",
        "auth_required": True,
        "rate_limited": False,
        "operation_id": "validateConfig",
        "summary": "Dry-run validation for setting values",
        "request_schema": {
            "type": "object",
            "required": ["env"],
            "properties": {
                "env": {
                    "type": "object",
                    "description": "Mapping of setting names to string values.",
                    "maxProperties": 50,
                    "additionalProperties": {"type": "string"},
                }
            },
            "additionalProperties": False,
        },
        "max_body_bytes": 262144,
        "responses": ["200", "400", "401", "403", "411", "413", "415"],
        "response_schemas": {
            "200": {
                "type": "object",
                "required": ["valid", "errors", "unknown"],
                "properties": {
                    "valid": {"type": "boolean"},
                    "errors": {
                        "type": "array",
                        "items": _CONFIG_ERROR_SCHEMA,
                    },
                    "unknown": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
            }
        },
        "error_responses": {
            "400": "Malformed settings validation input (validation_error)."
        },
    },
    {
        "method": "GET",
        "path": "/admin/rate-limits",
        "handler": list_rate_limits,
        "role": "admin",
        "auth_required": True,
        "rate_limited": False,
        "operation_id": "listRateLimits",
        "summary": "Read the default rate limit policy and overrides",
        "responses": ["200", "401", "403"],
        "response_schemas": {"200": {"$ref": "#/components/schemas/RateLimitList"}},
    },
    {
        "method": "PUT",
        "path": "/admin/rate-limits/{identity}",
        "handler": put_rate_limit,
        "body": True,
        "role": "admin",
        "auth_required": True,
        "rate_limited": False,
        "operation_id": "putRateLimit",
        "summary": "Set a rate limit override and reset its bucket",
        "parameters": [
            {
                "name": "identity",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "maxLength": 71,
                    "pattern": "^(key|client|ip):[A-Za-z0-9._:-]{1,64}$",
                },
            }
        ],
        "request_schema": SCHEMAS["RateLimitOverrideRequest"],
        "responses": ["200", "400", "401", "403", "409", "411", "413", "415"],
        "response_schemas": {"200": {"$ref": "#/components/schemas/RateLimitOverride"}},
        "error_responses": {
            "400": "The identity or override values are invalid (validation_error).",
            "409": "The override limit has been reached (override_limit).",
        },
    },
    {
        "method": "DELETE",
        "path": "/admin/rate-limits/{identity}",
        "handler": delete_rate_limit,
        "role": "admin",
        "auth_required": True,
        "rate_limited": False,
        "operation_id": "deleteRateLimit",
        "summary": "Remove a rate limit override and reset its bucket",
        "parameters": [
            {
                "name": "identity",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "maxLength": 71,
                    "pattern": "^(key|client|ip):[A-Za-z0-9._:-]{1,64}$",
                },
            }
        ],
        "responses": ["204", "400", "401", "403", "404"],
        "error_responses": {
            "400": "The identity is invalid (validation_error).",
            "404": "The override does not exist (override_not_found).",
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
                "tenants": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 10,
                    "uniqueItems": True,
                    "items": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,23}$"},
                },
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
        "method": "POST",
        "path": "/jobs",
        "handler": create_job,
        "body": True,
        "role": "write",
        "auth_required": True,
        "operation_id": "createJob",
        "summary": "Queue a background job",
        "request_schema": SCHEMAS["CreateJob"],
        "responses": ["202", "400", "401", "403", "503"],
        "response_schemas": {"202": {"$ref": "#/components/schemas/Job"}},
        "response_headers": {
            "202": {
                "Location": {
                    "description": "Relative URL of the queued job.",
                    "schema": {"type": "string"},
                }
            },
            "503": {
                "Retry-After": {
                    "description": "Retry after one second when the queue is full.",
                    "schema": {"type": "string", "enum": ["1"]},
                }
            },
        },
        "error_responses": {
            "400": "The job type or parameters are invalid.",
            "503": "The queued job limit is reached (queue_full).",
        },
    },
    {
        "method": "GET",
        "path": "/jobs",
        "handler": list_jobs,
        "role": None,
        "auth_required": False,
        "operation_id": "listJobs",
        "summary": "List background jobs",
        "parameters": _JOB_LIST_QUERY_PARAMETERS,
        "responses": ["200", "400"],
        "response_schemas": {"200": {"$ref": "#/components/schemas/JobList"}},
        "error_responses": {"400": "Invalid or repeated filter parameter."},
    },
    {
        "method": "GET",
        "path": "/jobs/{id}",
        "handler": get_job,
        "role": None,
        "auth_required": False,
        "operation_id": "getJob",
        "summary": "Read a background job",
        "parameters": [
            {
                "name": "id",
                "in": "path",
                "required": True,
                "schema": {"type": "integer", "minimum": 1},
            },
            *_JOB_WAIT_QUERY_PARAMETERS,
        ],
        "responses": ["200", "400", "404"],
        "response_schemas": {"200": {"$ref": "#/components/schemas/Job"}},
        "error_responses": {
            "400": "wait_ms must be an integer from 0 through 5000.",
            "404": "The job does not exist (job_not_found).",
        },
    },
    {
        "method": "POST",
        "path": "/jobs/{id}/cancel",
        "handler": cancel_job,
        "role": "write",
        "auth_required": True,
        "operation_id": "cancelJob",
        "summary": "Cancel a background job",
        "parameters": [
            {
                "name": "id",
                "in": "path",
                "required": True,
                "schema": {"type": "integer", "minimum": 1},
            }
        ],
        "responses": ["200", "401", "403", "404", "409"],
        "response_schemas": {"200": {"$ref": "#/components/schemas/Job"}},
        "error_responses": {
            "404": "The job does not exist (job_not_found).",
            "409": "The job is already terminal (job_not_cancellable).",
        },
    },
    {
        "method": "POST",
        "path": "/webhooks",
        "handler": create_webhook,
        "body": True,
        "role": "write",
        "auth_required": True,
        "operation_id": "createWebhook",
        "summary": "Register a simulated webhook destination",
        "request_schema": SCHEMAS["CreateWebhook"],
        "responses": ["201", "400", "401", "409", "413", "415", "403"],
        "response_schemas": {"201": {"$ref": "#/components/schemas/Webhook"}},
        "error_responses": {
            "409": "The webhook limit has been reached (webhook_limit)."
        },
    },
    {
        "method": "GET",
        "path": "/webhooks",
        "handler": list_webhooks,
        "role": "read",
        "auth_required": True,
        "operation_id": "listWebhooks",
        "summary": "List configured webhooks",
        "responses": ["200", "401", "403"],
        "response_schemas": {"200": {"$ref": "#/components/schemas/WebhookList"}},
    },
    {
        "method": "GET",
        "path": "/webhooks/{id}",
        "handler": get_webhook,
        "role": "read",
        "auth_required": True,
        "operation_id": "getWebhook",
        "summary": "Read one webhook",
        "parameters": [_OUTBOX_ID],
        "responses": ["200", "400", "401", "403", "404"],
        "response_schemas": {"200": {"$ref": "#/components/schemas/Webhook"}},
        "error_responses": {"404": "The webhook does not exist (webhook_not_found)."},
    },
    {
        "method": "PATCH",
        "path": "/webhooks/{id}",
        "handler": patch_webhook,
        "body": True,
        "role": "write",
        "auth_required": True,
        "operation_id": "updateWebhook",
        "summary": "Update webhook activation or events",
        "parameters": [_OUTBOX_ID],
        "request_schema": SCHEMAS["PatchWebhook"],
        "responses": ["200", "400", "401", "403", "404", "413", "415"],
        "response_schemas": {"200": {"$ref": "#/components/schemas/Webhook"}},
        "error_responses": {"404": "The webhook does not exist (webhook_not_found)."},
    },
    {
        "method": "DELETE",
        "path": "/webhooks/{id}",
        "handler": delete_webhook,
        "role": "write",
        "auth_required": True,
        "operation_id": "deleteWebhook",
        "summary": "Delete a webhook",
        "parameters": [_OUTBOX_ID],
        "responses": ["204", "400", "401", "403", "404"],
        "error_responses": {"404": "The webhook does not exist (webhook_not_found)."},
    },
    {
        "method": "GET",
        "path": "/outbox",
        "handler": list_outbox,
        "role": "read",
        "auth_required": True,
        "operation_id": "listOutbox",
        "summary": "List webhook delivery records",
        "parameters": _OUTBOX_QUERY_PARAMETERS,
        "responses": ["200", "400", "401", "403"],
        "response_schemas": {"200": {"$ref": "#/components/schemas/OutboxList"}},
    },
    {
        "method": "GET",
        "path": "/outbox/{id}",
        "handler": get_outbox,
        "role": "read",
        "auth_required": True,
        "operation_id": "getOutboxEntry",
        "summary": "Read one webhook delivery record",
        "parameters": [_OUTBOX_ID],
        "responses": ["200", "400", "401", "403", "404"],
        "response_schemas": {"200": {"$ref": "#/components/schemas/OutboxEntry"}},
        "error_responses": {"404": "The record does not exist (outbox_not_found)."},
    },
    {
        "method": "POST",
        "path": "/outbox/process",
        "handler": process_outbox,
        "body": True,
        "role": "admin",
        "auth_required": True,
        "operation_id": "processOutbox",
        "summary": "Synchronously process due webhook deliveries",
        "request_schema": SCHEMAS["ProcessOutbox"],
        "responses": ["200", "400", "401", "403", "413", "415"],
        "response_schemas": {
            "200": {"$ref": "#/components/schemas/ProcessOutboxResult"}
        },
    },
    {
        "method": "POST",
        "path": "/outbox/{id}/requeue",
        "handler": requeue_outbox,
        "role": "write",
        "auth_required": True,
        "operation_id": "requeueOutboxEntry",
        "summary": "Requeue a failed webhook delivery",
        "parameters": [_OUTBOX_ID],
        "responses": ["200", "400", "401", "403", "404", "409"],
        "response_schemas": {"200": {"$ref": "#/components/schemas/OutboxEntry"}},
        "error_responses": {
            "409": "Only failed entries can be requeued (not_requeueable)."
        },
    },
    {
        "method": "GET",
        "path": "/outbox/dispatcher",
        "handler": get_dispatcher,
        "role": "admin",
        "auth_required": True,
        "operation_id": "getOutboxDispatcher",
        "summary": "Read the webhook dispatcher configuration",
        "responses": ["200", "401", "403"],
        "response_schemas": {"200": {"$ref": "#/components/schemas/Dispatcher"}},
    },
    {
        "method": "PUT",
        "path": "/outbox/dispatcher",
        "handler": put_dispatcher,
        "body": True,
        "role": "admin",
        "auth_required": True,
        "operation_id": "configureOutboxDispatcher",
        "summary": "Configure the webhook dispatcher",
        "request_schema": SCHEMAS["UpdateDispatcher"],
        "responses": ["200", "400", "401", "403", "413", "415"],
        "response_schemas": {"200": {"$ref": "#/components/schemas/Dispatcher"}},
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
            },
            {
                "name": "tenant",
                "in": "query",
                "required": False,
                "description": "Filter by tenant; '*' includes all tenants.",
                "schema": {
                    "type": "string",
                    "maxLength": 24,
                    "pattern": "^(\\*|[a-z0-9][a-z0-9-]{0,23})$",
                },
            },
        ],
        "responses": ["200", "401", "403", "404"],
        "response_schemas": {"200": _AUDIT_ENTRY_SCHEMA},
        "error_responses": {
            "404": "The sequence is not retained (audit_entry_not_found)."
        },
    },
    {
        "method": "GET",
        "path": "/admin/tenants",
        "handler": list_tenants,
        "role": "admin",
        "auth_required": True,
        "rate_limited": False,
        "operation_id": "listTenants",
        "summary": "List tenants and resource counts",
        "responses": ["200", "401", "403"],
        "response_schemas": {
            "200": {
                "type": "object",
                "required": ["items", "total"],
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": [
                                "tenant",
                                "orders",
                                "products",
                                "webhooks",
                                "jobs",
                                "created_at",
                            ],
                            "properties": {
                                "tenant": {"type": "string"},
                                "orders": {"type": "integer", "minimum": 0},
                                "products": {"type": "integer", "minimum": 0},
                                "webhooks": {"type": "integer", "minimum": 0},
                                "jobs": {"type": "integer", "minimum": 0},
                                "created_at": {"type": "string"},
                            },
                        },
                    },
                    "total": {"type": "integer", "minimum": 0},
                },
            }
        },
    },
    {
        "method": "DELETE",
        "path": "/admin/tenants/{tenant}",
        "handler": delete_tenant,
        "role": "admin",
        "auth_required": True,
        "rate_limited": False,
        "operation_id": "deleteTenant",
        "summary": "Delete a tenant and its data",
        "parameters": [
            {
                "name": "tenant",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,23}$"},
            }
        ],
        "responses": ["204", "400", "401", "403", "404", "409"],
        "error_responses": {
            "404": "The tenant does not exist (tenant_not_found).",
            "409": "The default tenant is protected (default_tenant_protected).",
        },
    },
    {
        "method": "GET",
        "path": "/admin/traces",
        "handler": list_traces,
        "role": "admin",
        "auth_required": True,
        "rate_limited": False,
        "operation_id": "listTraces",
        "summary": "List retained request traces",
        "parameters": [
            {
                "name": "route",
                "in": "query",
                "required": False,
                "description": "Route template or the value 'unmatched'.",
                "schema": {"type": "string", "maxLength": 128},
            },
            {
                "name": "status",
                "in": "query",
                "required": False,
                "schema": {"type": "integer"},
            },
            {
                "name": "min_duration_ms",
                "in": "query",
                "required": False,
                "schema": {
                    "type": "number",
                    "minimum": 0,
                },
            },
            {
                "name": "limit",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        ],
        "responses": ["200", "400", "401", "403"],
        "response_schemas": {"200": {"$ref": "#/components/schemas/TraceList"}},
        "error_responses": {"400": "Invalid trace filter (invalid_query)."},
    },
    {
        "method": "GET",
        "path": "/admin/traces/{trace_id}",
        "handler": get_trace,
        "role": "admin",
        "auth_required": True,
        "rate_limited": False,
        "operation_id": "getTrace",
        "summary": "Read one retained request trace",
        "parameters": [
            {
                "name": "trace_id",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{32}$",
                },
            }
        ],
        "responses": ["200", "401", "403", "404"],
        "response_schemas": {"200": {"$ref": "#/components/schemas/Trace"}},
        "error_responses": {"404": "The trace is not retained (trace_not_found)."},
    },
    *_CSV_ROUTES,
)

_TENANT_SCOPED_PREFIXES = (
    "/orders",
    "/products",
    "/categories",
    "/jobs",
    "/webhooks",
    "/outbox",
    "/audit",
    "/exports",
    "/imports",
    "/search",
    "/v2/orders",
)
ROUTES = tuple(
    {
        **route,
        "tenant_scoped": any(
            str(route["path"]) == prefix or str(route["path"]).startswith(prefix + "/")
            for prefix in _TENANT_SCOPED_PREFIXES
        ),
    }
    for route in ROUTES
)
