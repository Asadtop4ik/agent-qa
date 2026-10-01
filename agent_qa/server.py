"""HTTP server adapter for the route handlers."""

import hashlib
import json
import logging
import math
import re
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import perf_counter
from urllib.parse import parse_qsl, urlsplit

from agent_qa.accesslog import write_access_log
from agent_qa.audit import AUDIT_LOG
from agent_qa import config
from agent_qa.auth import api_key_from_headers, authenticate_api_key
from agent_qa.errors import ApiError, envelope
from agent_qa.idempotency import IdempotencyStore, StoredResponse
from agent_qa.metrics import REGISTRY
from agent_qa.context import (
    RequestContext,
    clear_context,
    get_context,
    set_context,
)
from agent_qa.orders import OrderError
from agent_qa.outbox import OUTBOX
from agent_qa.request_id import request_id
from agent_qa.routes import JOB_RUNNER, ROUTES
from agent_qa.validation import validate

LOGGER = logging.getLogger(__name__)
IDEMPOTENCY_STORE = IdempotencyStore(config.idempotency_ttl_seconds())
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def _match_path(template: str, path: str) -> dict[str, str] | None:
    """Match a route path template and return its path parameters."""
    template_parts = template.split("/")
    path_parts = path.split("/")
    if len(template_parts) != len(path_parts):
        return None
    params: dict[str, str] = {}
    for expected, actual in zip(template_parts, path_parts):
        if expected.startswith("{") and expected.endswith("}"):
            name = expected[1:-1]
            if not name or not actual:
                return None
            params[name] = actual
        elif expected != actual:
            return None
    return params


def _path_routes(path: str) -> list[tuple[dict[str, object], dict[str, str]]]:
    matches = []
    for route in ROUTES:
        params = _match_path(str(route["path"]), path)
        if params is not None:
            matches.append((route, params))
    if matches:
        literal_count = max(
            sum(
                not (part.startswith("{") and part.endswith("}"))
                for part in str(route["path"]).split("/")
            )
            for route, _ in matches
        )
        matches = [
            (route, params)
            for route, params in matches
            if sum(
                not (part.startswith("{") and part.endswith("}"))
                for part in str(route["path"]).split("/")
            )
            == literal_count
        ]
    return matches


def allowed_methods(path: str) -> str:
    routes = _path_routes(path)
    methods = {str(route["method"]) for route, _ in routes}
    return ", ".join(sorted(methods))


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _parse_finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON numbers must be finite")
    return number


class Handler(BaseHTTPRequestHandler):
    def handle_one_request(self) -> None:
        self.request_id = request_id(None)
        set_context(RequestContext(request_id=self.request_id))
        self._request_started = perf_counter()
        self._response_recorded = False
        try:
            super().handle_one_request()
        finally:
            clear_context()

    def parse_request(self) -> bool:
        parsed = super().parse_request()
        self._use_header_request_id()
        return parsed

    def _use_header_request_id(self) -> None:
        headers = getattr(self, "headers", None)
        if headers is not None:
            self.request_id = request_id(headers.get("X-Request-Id"))
            context = get_context()
            if context is not None:
                context.request_id = self.request_id

    def _json(
        self, status: int, body: object, headers: dict[str, str] | None = None
    ) -> None:
        is_empty = status in {204, 304}
        response_headers = headers or {}
        content_type = response_headers.get("Content-Type")
        if is_empty:
            encoded = b""
        elif content_type and isinstance(body, str):
            encoded = body.encode("utf-8")
        else:
            encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self._audit_response_body = body
        self._record_response(status)
        self.send_response(status)
        if not is_empty:
            self.send_header(
                "Content-Type", content_type or "application/json; charset=utf-8"
            )
        if status != 304:
            self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-Id", self.request_id)
        for name, value in response_headers.items():
            if name.lower() not in {
                "content-length",
                "content-type",
                "x-request-id",
            }:
                self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD" and encoded:
            self.wfile.write(encoded)

    def _record_response(self, status: int) -> None:
        if self._response_recorded:
            return
        self._response_recorded = True
        raw_path = getattr(self, "path", "") or ""
        try:
            path = urlsplit(raw_path).path
        except ValueError:
            path = ""
        matches = _path_routes(path)
        route = str(matches[0][0]["path"]) if matches else "unmatched"
        method = getattr(self, "command", "") or ""
        audit_route = next(
            (
                candidate
                for candidate, _ in matches
                if candidate.get("method") == method
            ),
            None,
        )
        if (
            status != 405
            and method in {"POST", "PUT", "PATCH", "DELETE"}
            and audit_route is not None
        ):
            context = get_context()
            if context is None:
                context = RequestContext(
                    request_id=getattr(self, "request_id", "") or ""
                )
            response_body = getattr(self, "_audit_response_body", None)
            if context.resource_id is None and isinstance(response_body, dict):
                created_id = response_body.get("id")
                if context.resource == "keys":
                    created_id = response_body.get("key_id")
                if isinstance(created_id, (int, str)) and not isinstance(
                    created_id, bool
                ):
                    context.resource_id = created_id
            AUDIT_LOG.append(
                context,
                method,
                str(audit_route["path"]),
                path,
                status,
                replay=getattr(self, "_audit_replay", False),
            )
        if hasattr(self, "_audit_response_body"):
            del self._audit_response_body
        if hasattr(self, "_audit_replay"):
            del self._audit_replay
        duration = perf_counter() - self._request_started
        REGISTRY.record(method, route, status, duration)
        write_access_log(
            method,
            path,
            route,
            status,
            duration,
            getattr(self, "request_id", ""),
        )

    def _not_found(self) -> None:
        self._json(
            404, envelope("not_found", "Route not found", request_id=self.request_id)
        )

    def _internal_error(self) -> None:
        self._json(
            500,
            envelope(
                "internal_error", "Internal server error", request_id=self.request_id
            ),
        )

    def _read_json_body(
        self, require_object: bool = True, max_body_bytes: int = 4096
    ) -> object:
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise ApiError(411, "length_required", "Content-Length is required")
        try:
            length = int(length_header)
        except ValueError as error:
            raise ApiError(
                400, "invalid_json", "Request body must be valid JSON"
            ) from error
        if length < 0:
            raise ApiError(400, "invalid_json", "Request body must be valid JSON")
        if (
            isinstance(max_body_bytes, bool)
            or not isinstance(max_body_bytes, int)
            or max_body_bytes < 1
        ):
            raise RuntimeError("Invalid route max_body_bytes configuration")
        if length > max_body_bytes:
            raise ApiError(413, "payload_too_large", "Request body is too large")
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise ApiError(
                415, "unsupported_media_type", "Content-Type must be application/json"
            )
        try:
            body = self.rfile.read(length).decode("utf-8")
            payload = json.loads(
                body,
                parse_constant=_reject_json_constant,
                parse_float=_parse_finite_float,
            )
        except (UnicodeDecodeError, ValueError, RecursionError) as error:
            raise ApiError(
                400, "invalid_json", "Request body must be valid JSON"
            ) from error
        if require_object and not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "Request body must be a JSON object")
        pending = [(payload, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > 32:
                raise ApiError(400, "invalid_json", "JSON nesting is too deep")
            if isinstance(value, dict):
                pending.extend((child, depth + 1) for child in value.values())
            elif isinstance(value, list):
                pending.extend((child, depth + 1) for child in value)
        return payload

    def _dispatch(self) -> None:
        parsed = urlsplit(self.path)
        matches = _path_routes(parsed.path)
        if not matches:
            self._not_found()
            return
        selected = next(
            (match for match in matches if match[0]["method"] == self.command), None
        )
        if selected is None:
            self._method_not_allowed(matches)
            return
        route, path_params = selected
        context = get_context()
        if context is not None:
            segments = str(route["path"]).strip("/").split("/")
            context.resource = next(
                (
                    segment
                    for segment in segments
                    if segment in {"orders", "products", "keys", "jobs"}
                ),
                None,
            )
            path_id = next(
                (
                    value
                    for name, value in path_params.items()
                    if name.endswith("_id") or name == "id"
                ),
                None,
            )
            if path_id is not None:
                if context.resource == "keys":
                    context.resource_id = path_id[:256]
                else:
                    try:
                        if len(path_id) <= 19:
                            context.resource_id = int(path_id)
                        else:
                            context.resource_id = path_id[:256]
                    except ValueError:
                        context.resource_id = path_id[:256]
        required_role = route.get(
            "role", "write" if route.get("auth_required") else None
        )
        identity = None
        if required_role is not None or self.command in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }:
            identity = authenticate_api_key(self.headers)
        if context is not None and identity is not None:
            context.actor = identity.get("key_id", "anonymous")
            context.role = identity.get("role")
        if required_role is not None and identity is None:
            self._json(
                401,
                envelope(
                    "unauthorized",
                    "Invalid or missing API key",
                    request_id=self.request_id,
                ),
                {"WWW-Authenticate": "X-API-Key"},
            )
            return
        role_rank = {"read": 1, "write": 2, "admin": 3}
        if (
            required_role is not None
            and identity is not None
            and role_rank.get(identity["role"], 0) < role_rank.get(required_role, 4)
        ):
            self._json(
                403,
                envelope(
                    "forbidden",
                    "Insufficient API key role",
                    [{"field": "role", "message": f"Requires role {required_role}"}],
                    request_id=self.request_id,
                ),
            )
            return
        idempotency_key: str | None = None
        idempotency_scope: tuple[str, ...] | None = None
        if self.command == "POST" and route.get("idempotent"):
            idempotency_key = self.headers.get("Idempotency-Key")
            key_values = self.headers.get_all("Idempotency-Key", [])
            if len(key_values) > 1 or (
                idempotency_key is not None
                and not _IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key)
            ):
                raise ApiError(
                    400,
                    "invalid_idempotency_key",
                    "Idempotency-Key must be 1-64 permitted characters",
                )
        query = parse_qsl(parsed.query, keep_blank_values=True)
        payload = (
            self._read_json_body(
                require_object=route.get("json_object_only", True),
                max_body_bytes=route.get("max_body_bytes", 4096),
            )
            if route.get("body")
            else None
        )
        if idempotency_key is not None:
            canonical_json = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            payload_fingerprint = hashlib.sha256(canonical_json).hexdigest()
            api_key = api_key_from_headers(self.headers) or ""
            api_key_fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
            idempotency_scope = (
                api_key_fingerprint,
                self.command,
                parsed.path,
                idempotency_key,
            )
            decision = IDEMPOTENCY_STORE.begin(idempotency_scope, payload_fingerprint)
            if decision.kind == "replay":
                REGISTRY.record_idempotency("replayed")
                assert decision.response is not None
                replay_headers = dict(decision.response.headers)
                replay_headers["Idempotent-Replay"] = "true"
                replay_headers["Idempotency-Key"] = idempotency_key
                self._audit_replay = True
                self._audit_response_body = decision.response.body
                self._json(
                    decision.response.status,
                    decision.response.body,
                    replay_headers,
                )
                return
            if decision.kind == "mismatch":
                REGISTRY.record_idempotency("mismatch")
                raise ApiError(
                    422,
                    "idempotency_key_reused",
                    "Idempotency-Key was already used with a different request",
                )
            if decision.kind == "in_progress":
                REGISTRY.record_idempotency("in_progress")
                raise ApiError(
                    409,
                    "idempotency_in_progress",
                    "A request with this Idempotency-Key is already in progress",
                )
        try:
            schema = route.get("request_schema")
            if schema is not None:
                errors = validate(schema, payload)
                if errors:
                    if route.get("sanitize_validation_errors"):
                        errors = [{"field": "body", "message": "Invalid request body"}]
                    raise ApiError(
                        400,
                        "validation_error",
                        "Request validation failed",
                        errors,
                    )
            conditional_headers = route.get("conditional_headers")
            if conditional_headers:
                request_headers = {}
                for name in conditional_headers:
                    values = self.headers.get_all(name, [])
                    if values:
                        request_headers[name] = ", ".join(values)
                status, body, headers = route["handler"](
                    query, path_params, payload, request_headers=request_headers
                )
            elif route.get("pass_identity"):
                status, body, headers = route["handler"](
                    query, path_params, payload, identity=identity
                )
            else:
                status, body, headers = route["handler"](query, path_params, payload)
        except Exception:
            if idempotency_scope is not None:
                IDEMPOTENCY_STORE.abort(idempotency_scope)
            raise
        if idempotency_scope is not None:
            replay_statuses = route.get("idempotency_replay_statuses", ())
            if 200 <= status < 300 or status in replay_statuses:
                IDEMPOTENCY_STORE.complete(
                    idempotency_scope,
                    StoredResponse(status, body, headers),
                    allowed_statuses=tuple(replay_statuses),
                )
                REGISTRY.record_idempotency("stored")
                headers = dict(headers)
                headers["Idempotency-Key"] = idempotency_key or ""
            else:
                IDEMPOTENCY_STORE.abort(idempotency_scope)
        self._json(status, body, headers)

    def _method_not_allowed(
        self, routes: list[tuple[dict[str, object], dict[str, str]]]
    ) -> None:
        methods = sorted({str(route["method"]) for route, _ in routes})
        self._json(
            405,
            envelope(
                "method_not_allowed", "Method not allowed", request_id=self.request_id
            ),
            {"Allow": ", ".join(methods)},
        )

    def _handle_get(self) -> None:
        self._dispatch()

    def _handle(self, dispatch: Callable[[], None] | None = None) -> None:
        try:
            (dispatch or self._dispatch)()
        except ApiError as error:
            response_headers = {}
            if error.code == "queue_full":
                response_headers["Retry-After"] = "1"
            current_etag = getattr(error, "current_etag", None)
            if current_etag is not None:
                response_headers["ETag"] = current_etag
            self._json(
                error.status,
                envelope(
                    error.code,
                    error.message,
                    error.details,
                    request_id=self.request_id,
                ),
                response_headers,
            )
        except OrderError as error:
            status = {
                "validation_error": 400,
                "invalid_query": 400,
                "store_full": 409,
                "order_locked": 409,
                "invalid_transition": 409,
            }.get(error.code, 500)
            self._json(
                status,
                envelope(
                    error.code,
                    error.message,
                    error.details,
                    request_id=self.request_id,
                ),
            )
        except Exception:
            LOGGER.exception("Unhandled request exception")
            self._internal_error()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle(self._handle_get)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle()

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle()

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle()

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle()

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        self._use_header_request_id()
        safe_message = "Bad request" if code < 500 else "Internal server error"
        self._json(
            code,
            envelope("http_error", safe_message, request_id=self.request_id),
        )

    def log_message(self, fmt: str, *args: object) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", config.port()), Handler)
    try:
        server.serve_forever()
    finally:
        JOB_RUNNER.stop(timeout=5)
        OUTBOX.stop(timeout=5)
        server.server_close()
