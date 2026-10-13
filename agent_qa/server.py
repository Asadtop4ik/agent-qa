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
from agent_qa import config
from agent_qa.auth import is_valid_api_key
from agent_qa.errors import ApiError, envelope
from agent_qa.idempotency import BeginKind, IdempotencyStore, Response
from agent_qa.metrics import REGISTRY
from agent_qa.orders import OrderError
from agent_qa.request_id import request_id
from agent_qa import routes as routes_module
from agent_qa.routes import ROUTES
from agent_qa.validation import validate


LOGGER = logging.getLogger(__name__)
IDEMPOTENCY_STORE = IdempotencyStore(config.idempotency_ttl_seconds())
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]+$")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("JSON number is outside the supported range")
    return parsed


def _has_bounded_json_depth(payload: object, maximum: int = 64) -> bool:
    pending = [(payload, 0)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, (dict, list)):
            if depth >= maximum:
                return False
            children = value.values() if isinstance(value, dict) else value
            pending.extend((child, depth + 1) for child in children)
    return True


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
    return matches


def allowed_methods(path: str) -> str:
    routes = _path_routes(path)
    methods = {str(route["method"]) for route, _ in routes}
    return ", ".join(sorted(methods))


class Handler(BaseHTTPRequestHandler):
    def handle_one_request(self) -> None:
        self.request_id = request_id(None)
        self._request_started = perf_counter()
        self._response_recorded = False
        super().handle_one_request()

    def parse_request(self) -> bool:
        parsed = super().parse_request()
        self._use_header_request_id()
        return parsed

    def _use_header_request_id(self) -> None:
        headers = getattr(self, "headers", None)
        if headers is not None:
            self.request_id = request_id(headers.get("X-Request-Id"))

    def _json(
        self, status: int, body: object, headers: dict[str, str] | None = None
    ) -> None:
        is_empty = status == 204
        response_headers = headers or {}
        content_type = response_headers.get("Content-Type")
        if is_empty:
            encoded = b""
        elif content_type and isinstance(body, str):
            encoded = body.encode("utf-8")
        else:
            encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self._record_response(status)
        self.send_response(status)
        if not is_empty:
            self.send_header(
                "Content-Type", content_type or "application/json; charset=utf-8"
            )
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-Id", self.request_id)
        for name, value in response_headers.items():
            if name.lower() not in {"content-length", "content-type"}:
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

    def _read_json_body(self) -> object:
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
        if length > 4096:
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
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            ValueError,
        ) as error:
            raise ApiError(
                400, "invalid_json", "Request body must be valid JSON"
            ) from error
        if not _has_bounded_json_depth(payload):
            raise ApiError(400, "invalid_json", "Request body must be valid JSON")
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
        if route["auth_required"] and (
            not self.headers.get("X-API-Key") or not is_valid_api_key(self.headers)
        ):
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
        idempotency_key = self.headers.get("Idempotency-Key")
        idempotent = bool(route.get("idempotent") and idempotency_key is not None)
        if idempotent and (
            not 1 <= len(idempotency_key) <= 64
            or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None
        ):
            raise ApiError(
                400,
                "invalid_idempotency_key",
                "Idempotency-Key must contain 1 to 64 valid characters",
            )
        query = parse_qsl(parsed.query, keep_blank_values=True)
        payload = self._read_json_body() if route.get("body") else None
        if idempotent:
            canonical_body = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            fingerprint = hashlib.sha256(canonical_body).hexdigest()
            api_key = self.headers.get("X-API-Key", "")
            key_fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
            scope = (key_fingerprint, self.command, parsed.path, idempotency_key)
            result = IDEMPOTENCY_STORE.begin(scope, fingerprint)
            if result.kind is BeginKind.REPLAY:
                routes_module.REGISTRY.record_idempotency("replayed")
                assert result.response is not None
                replay_headers = dict(result.response.headers)
                replay_headers.update(
                    {
                        "Idempotent-Replay": "true",
                        "Idempotency-Key": idempotency_key,
                    }
                )
                self._json(
                    result.response.status,
                    result.response.body,
                    replay_headers,
                )
                return
            if result.kind is BeginKind.MISMATCH:
                routes_module.REGISTRY.record_idempotency("mismatch")
                raise ApiError(
                    422,
                    "idempotency_key_reused",
                    "Idempotency-Key was already used with a different request",
                )
            if result.kind is BeginKind.IN_PROGRESS:
                routes_module.REGISTRY.record_idempotency("in_progress")
                raise ApiError(
                    409,
                    "idempotency_in_progress",
                    "A request with this Idempotency-Key is still in progress",
                )
            try:
                self._validate_payload(route, parsed.path, payload)
                status, body, headers = route["handler"](query, path_params, payload)
                response_headers = dict(headers)
                response_headers = {
                    name: value
                    for name, value in response_headers.items()
                    if name.lower() != "x-request-id"
                }
                response = Response(status, body, response_headers)
            except Exception:
                IDEMPOTENCY_STORE.abort(scope, fingerprint)
                raise
            if 200 <= status < 300:
                IDEMPOTENCY_STORE.complete(scope, fingerprint, response)
                routes_module.REGISTRY.record_idempotency("stored")
            else:
                IDEMPOTENCY_STORE.abort(scope, fingerprint)
            self._json(status, body, headers)
            return
        self._validate_payload(route, parsed.path, payload)
        status, body, headers = route["handler"](query, path_params, payload)
        self._json(status, body, headers)

    def _validate_payload(
        self, route: dict[str, object], path: str, payload: object
    ) -> None:
        request_schema = route.get("request_schema")
        if request_schema is None:
            return
        errors = validate(request_schema, payload)
        if errors:
            if path == "/orders" or path.startswith("/orders/"):
                errors = sorted(
                    errors,
                    key=lambda detail: (
                        detail["message"] != "Unknown field",
                        detail["field"],
                    ),
                )
            raise ApiError(
                400,
                "validation_error",
                "Request validation failed",
                errors,
            )

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
            self._json(
                error.status,
                envelope(
                    error.code,
                    error.message,
                    error.details,
                    request_id=self.request_id,
                ),
            )
        except OrderError as error:
            status = {
                "validation_error": 400,
                "invalid_query": 400,
                "store_full": 409,
                "duplicate_sku": 409,
                "insufficient_stock": 409,
                "product_not_found": 404,
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
            try:
                path = urlsplit(getattr(self, "path", "")).path
            except ValueError:
                path = "<invalid>"
            LOGGER.exception(
                "Unhandled request error method=%s path=%s",
                getattr(self, "command", ""),
                path,
            )
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
    ThreadingHTTPServer(("0.0.0.0", config.port()), Handler).serve_forever()
