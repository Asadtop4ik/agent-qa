"""HTTP server adapter for the route handlers."""

import gzip
import hashlib
import ipaddress
import json
import logging
import math
import re
import signal
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import perf_counter
from urllib.parse import parse_qsl, urlsplit

from agent_qa.accesslog import write_access_log
from agent_qa.audit import AUDIT_LOG
from agent_qa import settings
from agent_qa.auth import api_key_from_headers, authenticate_api_key
from agent_qa.errors import ApiError, envelope, problem
from agent_qa.idempotency import IdempotencyStore, StoredResponse
from agent_qa.metrics import REGISTRY
from agent_qa.maintenance import MAINTENANCE
from agent_qa.context import (
    RequestContext,
    clear_context,
    get_context,
    set_context,
)
from agent_qa.orders import OrderError
from agent_qa.request_id import request_id
from agent_qa.ratelimit import RATE_LIMITER
from agent_qa import tenants
from agent_qa.negotiation import (
    MIN_GZIP_BYTES,
    best_match,
    gzip_acceptable,
    prefers_problem,
    parse_accept,
)
from agent_qa.routes import ROUTES
from agent_qa.validation import validate
from agent_qa.versioning import response_headers as version_headers
from agent_qa.versioning import map_v2_error_body, sunset_reached

LOGGER = logging.getLogger(__name__)
IDEMPOTENCY_STORE = tenants.get("default").idempotency
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_SHUTDOWN_REQUESTED = threading.Event()


class _RequestTracker:
    """Count executing HTTP requests without counting idle keep-alive sockets."""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.active = 0

    def begin(self) -> None:
        with self.condition:
            self.active += 1

    def finish(self) -> None:
        with self.condition:
            self.active -= 1
            self.condition.notify_all()

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)
            return True


_REQUESTS = _RequestTracker()


def _reset_shutdown_tracking() -> None:
    _SHUTDOWN_REQUESTED.clear()
    with _REQUESTS.condition:
        _REQUESTS.active = 0


def _idempotency_store() -> IdempotencyStore:
    context = get_context()
    if context is None or context.tenant == "default":
        return IDEMPOTENCY_STORE
    return tenants.current().idempotency


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


def _joined_header(headers: object, name: str) -> str | None:
    get_all = getattr(headers, "get_all", None)
    values = get_all(name, []) if get_all is not None else []
    if values:
        return ", ".join(values)
    return headers.get(name) if hasattr(headers, "get") else None


def _has_unsupported_content_encoding(values: list[str]) -> bool:
    for value in values:
        while True:
            coding, separator, value = value.partition(",")
            if coding.strip().lower() != "identity":
                return True
            if not separator:
                break
    return False


def _merge_vary(headers: dict[str, str], *names: str) -> None:
    vary_key = next((key for key in headers if key.lower() == "vary"), "Vary")
    existing = headers.get(vary_key, "")
    if existing.strip() == "*":
        return
    tokens = [token.strip() for token in existing.split(",") if token.strip()]
    seen = {token.lower() for token in tokens}
    for name in names:
        if name.lower() not in seen:
            tokens.append(name)
            seen.add(name.lower())
    if tokens:
        headers[vary_key] = ", ".join(tokens)


def _problem_body(
    status: int, body: object, path: str, request_id_value: str
) -> object:
    if not isinstance(body, dict) or not isinstance(body.get("error"), dict):
        return body
    error = body["error"]
    code = error.get("code", "http_error")
    message = error.get("message", "Request failed")
    if not isinstance(code, str):
        code = "http_error"
    if not isinstance(message, str):
        message = "Request failed"
    details = error.get("details")
    return problem(
        status,
        code,
        message,
        details,
        request_id=request_id_value,
        instance=path,
    )


class Handler(BaseHTTPRequestHandler):
    def handle_one_request(self) -> None:
        self.request_id = request_id(None)
        set_context(RequestContext(request_id=self.request_id))
        self._request_started = perf_counter()
        self._response_recorded = False
        self._rate_headers = {}
        self._tenant_scoped = False
        self._tenant_value = None
        self._tracked_request = False
        self._audit_rejected = False
        self._maintenance_retry_after = None
        if hasattr(self, "_rate_retry_after"):
            del self._rate_retry_after
        try:
            super().handle_one_request()
        finally:
            if self._tracked_request:
                self._tracked_request = False
                _REQUESTS.finish()
            clear_context()

    def parse_request(self) -> bool:
        parsed = super().parse_request()
        self._use_header_request_id()
        if parsed and not self._tracked_request:
            _REQUESTS.begin()
            self._tracked_request = True
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
        response_headers = dict(headers or {})
        tenant_value = getattr(self, "_tenant_value", None)
        if getattr(self, "_tenant_scoped", False) and tenant_value is not None:
            response_headers["X-Tenant"] = tenant_value
        raw_path = getattr(self, "path", "") or ""
        try:
            instance = urlsplit(raw_path).path
        except ValueError:
            instance = ""
        route_matches = _path_routes(instance)
        route_match = next(
            (
                (route, params)
                for route, params in route_matches
                if route.get("method") == getattr(self, "command", None)
            ),
            route_matches[0] if route_matches else (None, {}),
        )
        version_route, version_params = route_match
        if version_route is not None:
            generated = version_headers(version_route, version_params)
            for name, value in generated.items():
                if name.lower() == "link":
                    current_link = next(
                        (key for key in response_headers if key.lower() == "link"),
                        None,
                    )
                    if current_link is not None:
                        response_headers[current_link] = (
                            f"{response_headers[current_link]}, {value}"
                        )
                        continue
                response_headers[name] = value
        response_headers.update(getattr(self, "_rate_headers", {}))
        if status == 429 and hasattr(self, "_rate_retry_after"):
            response_headers["Retry-After"] = str(self._rate_retry_after)
        accept = _joined_header(getattr(self, "headers", None), "Accept")
        if status >= 400:
            _merge_vary(response_headers, "Accept")
            is_v2 = (
                version_route is not None
                and str(version_route.get("api_version")) == "2"
            )
            if is_v2:
                body = map_v2_error_body(body)
            if is_v2 or prefers_problem(accept):
                body = _problem_body(status, body, instance, self.request_id)
                response_headers["Content-Type"] = (
                    "application/problem+json; charset=utf-8"
                )
        content_type = response_headers.get("Content-Type")
        if is_empty:
            encoded = b""
        elif content_type and isinstance(body, str):
            encoded = body.encode("utf-8")
        else:
            encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        is_ready = instance == "/ready"
        if encoded and not is_ready:
            _merge_vary(response_headers, "Accept-Encoding")
            accept_encoding = _joined_header(
                getattr(self, "headers", None), "Accept-Encoding"
            )
            if len(encoded) >= MIN_GZIP_BYTES and gzip_acceptable(accept_encoding):
                encoded = gzip.compress(encoded, mtime=0)
                response_headers["Content-Encoding"] = "gzip"
                etag_key = next(
                    (key for key in response_headers if key.lower() == "etag"), None
                )
                if etag_key is not None:
                    etag = response_headers[etag_key]
                    if etag and not etag.startswith("W/"):
                        response_headers[etag_key] = f"W/{etag}"
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
                rejected=getattr(self, "_audit_rejected", False),
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

    def _prepare_tenant(self, scoped: bool) -> None:
        self._tenant_scoped = scoped
        self._tenant_value = None
        context = get_context()
        if not scoped:
            if context is not None:
                context.tenant = "default"
            return
        raw = _joined_header(self.headers, "X-Tenant")
        name = tenants.validate_name(raw if raw is not None else "default")
        self._tenant_value = name
        if context is not None:
            context.tenant = name

    def _internal_error(self) -> None:
        self._json(
            500,
            envelope(
                "internal_error", "Internal server error", request_id=self.request_id
            ),
        )

    def _read_request_body(
        self,
        consumes: list[str] | tuple[str, ...] = ("application/json",),
        require_object: bool = True,
        max_body_bytes: int = 4096,
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
        media_type, *parameters = content_type.split(";")
        media_type = media_type.strip().lower()
        if media_type not in consumes:
            raise ApiError(
                415,
                "unsupported_media_type",
                f"Content-Type must be {' or '.join(consumes)}",
            )
        charsets = []
        for parameter in parameters:
            key, separator, value = parameter.partition("=")
            if key.strip().lower() == "charset":
                charsets.append(value.strip().strip('"').lower() if separator else "")
        if media_type == "text/csv" and (
            len(charsets) > 1 or (charsets and charsets[0] != "utf-8")
        ):
            raise ApiError(415, "unsupported_media_type", "CSV charset must be UTF-8")
        try:
            raw = self.rfile.read(length)
            body = raw.decode("utf-8-sig" if media_type == "text/csv" else "utf-8")
            if media_type == "text/csv":
                return body
            payload = json.loads(
                body,
                parse_constant=_reject_json_constant,
                parse_float=_parse_finite_float,
            )
        except UnicodeDecodeError as error:
            code = "invalid_csv" if media_type == "text/csv" else "invalid_json"
            message = (
                "Request body must be valid UTF-8 CSV"
                if media_type == "text/csv"
                else "Request body must be valid JSON"
            )
            details = [{"field": "body", "message": "Must be valid UTF-8"}]
            raise ApiError(
                400,
                code,
                message,
                details if media_type == "text/csv" else None,
            ) from error
        except (ValueError, RecursionError) as error:
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

    def _read_json_body(
        self, require_object: bool = True, max_body_bytes: int = 4096
    ) -> object:
        """Compatibility wrapper retained for direct handler tests."""
        return self._read_request_body(
            ("application/json",), require_object, max_body_bytes
        )

    def _dispatch(self) -> None:
        self._tenant_write_pin = None
        try:
            Handler._dispatch_request(self)
        finally:
            pin = self._tenant_write_pin
            if pin is not None:
                self._tenant_write_pin = None
                pin.__exit__(*sys.exc_info())

    def _dispatch_request(self) -> None:
        Handler._prepare_tenant(self, False)
        try:
            parsed = urlsplit(self.path)
        except ValueError as error:
            raise ApiError(
                400, "invalid_request_target", "Request target is invalid"
            ) from error
        matches = _path_routes(parsed.path)
        if not matches:
            self._not_found()
            return
        selected = next(
            (match for match in matches if match[0]["method"] == self.command), None
        )
        if selected is None:
            Handler._prepare_tenant(
                self, any(bool(route.get("tenant_scoped")) for route, _ in matches)
            )
            is_v2_path = any(
                str(route.get("api_version")) == "2" for route, _ in matches
            )
            accept = _joined_header(self.headers, "Accept")
            if (
                is_v2_path
                and accept is not None
                and best_match(parse_accept(accept), ["application/json"]) is None
            ):
                self._json(
                    406,
                    envelope(
                        "not_acceptable",
                        "No acceptable representation is available",
                        request_id=self.request_id,
                    ),
                )
                return
            self._method_not_allowed(matches)
            return
        route, path_params = selected
        Handler._prepare_tenant(self, bool(route.get("tenant_scoped")))
        if sunset_reached(route):
            self._json(
                410,
                envelope(
                    "api_version_sunset",
                    "This API version has passed its sunset date",
                    request_id=self.request_id,
                ),
            )
            return
        accept = _joined_header(self.headers, "Accept")
        if accept is not None:
            produces = route.get("produces", ["application/json"])
            if not isinstance(produces, (list, tuple)):
                produces = ["application/json"]
            offered = list(produces)
            if "application/json" in offered:
                offered.append("application/problem+json")
            acceptable_types = (
                ["application/json"]
                if str(route.get("api_version")) == "2"
                else offered
            )
            if best_match(parse_accept(accept), acceptable_types) is None:
                self._json(
                    406,
                    envelope(
                        "not_acceptable",
                        "No acceptable representation is available",
                        request_id=self.request_id,
                    ),
                )
                return
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
        needs_authentication = (
            required_role is not None
            or self.command in {"POST", "PUT", "PATCH", "DELETE"}
            or route.get("rate_limited", True)
        )
        if needs_authentication:
            identity = authenticate_api_key(self.headers)
        if context is not None and identity is not None:
            context.actor = identity.get("key_id", "anonymous")
            context.role = identity.get("role")
        if route.get("rate_limited", True):
            key_id = identity.get("key_id") if identity is not None else None
            client_id = self.headers.get("X-Client-Id", "")
            if isinstance(key_id, str) and _KEY_ID_PATTERN.fullmatch(key_id):
                rate_kind, rate_identity = "key", f"key:{key_id}"
            elif len(client_id) <= 32 and _CLIENT_ID_PATTERN.fullmatch(client_id):
                rate_kind, rate_identity = "client", f"client:{client_id}"
            else:
                address = getattr(self, "client_address", ("unknown",))[0]
                try:
                    rate_ip = ipaddress.ip_address(address).compressed
                except (ValueError, TypeError):
                    rate_ip = "unknown"
                rate_kind, rate_identity = "ip", f"ip:{rate_ip}"
            rate_decision = RATE_LIMITER.consume(rate_identity, rate_kind)
            self._rate_headers = {
                "RateLimit-Limit": str(rate_decision.limit),
                "RateLimit-Remaining": str(rate_decision.remaining),
                "RateLimit-Reset": str(rate_decision.reset_after),
            }
            if not rate_decision.allowed:
                self._rate_retry_after = rate_decision.retry_after
                REGISTRY.record_rate_limited(rate_kind)
                raise ApiError(429, "rate_limited", "Rate limit exceeded")
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
        if route.get("tenant_scoped") and identity is not None:
            permitted_tenants = identity.get("tenants")
            tenant_name = context.tenant if context is not None else "default"
            if permitted_tenants is not None and tenant_name not in permitted_tenants:
                raise ApiError(
                    403,
                    "forbidden",
                    "API key is not allowed for this tenant",
                    [
                        {
                            "field": "tenant",
                            "message": "Key is not allowed for this tenant",
                        }
                    ],
                )
        if (
            self.command in {"POST", "PUT", "PATCH", "DELETE"}
            and route.get("path")
            and not str(route["path"]).startswith("/admin/")
        ):
            maintenance = MAINTENANCE.snapshot()
            if maintenance["enabled"]:
                self._audit_rejected = True
                self._maintenance_retry_after = maintenance["retry_after_seconds"]
                raise ApiError(
                    503,
                    "maintenance",
                    str(maintenance["message"]),
                )
        content_encodings = self.headers.get_all("Content-Encoding", [])
        if _has_unsupported_content_encoding(content_encodings):
            raise ApiError(
                415,
                "unsupported_content_encoding",
                "Content-Encoding must be identity",
            )
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
            self._read_request_body(
                consumes=route.get("consumes", ["application/json"]),
                require_object=route.get("json_object_only", True),
                max_body_bytes=route.get("max_body_bytes", 4096),
            )
            if route.get("body")
            else None
        )
        if route.get("tenant_scoped") and self.command in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }:
            context = get_context()
            bundle = tenants.ensure(
                context.tenant if context is not None else "default"
            )
            pin = tenants.TENANTS.pin(bundle)
            pin.__enter__()
            self._tenant_write_pin = pin
        if idempotency_key is not None:
            canonical_json = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            payload_fingerprint = hashlib.sha256(canonical_json).hexdigest()
            api_key = api_key_from_headers(self.headers) or ""
            api_key_fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
            idempotency_scope = (
                get_context().tenant if get_context() is not None else "default",
                api_key_fingerprint,
                self.command,
                parsed.path,
                idempotency_key,
            )
            decision = _idempotency_store().begin(
                idempotency_scope, payload_fingerprint
            )
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
                        400, "validation_error", "Request validation failed", errors
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
                _idempotency_store().abort(idempotency_scope)
            raise
        if idempotency_scope is not None:
            replay_statuses = route.get("idempotency_replay_statuses", ())
            if 200 <= status < 300 or status in replay_statuses:
                _idempotency_store().complete(
                    idempotency_scope,
                    StoredResponse(status, body, headers),
                    allowed_statuses=tuple(replay_statuses),
                )
                REGISTRY.record_idempotency("stored")
                headers = dict(headers)
                headers["Idempotency-Key"] = idempotency_key or ""
            else:
                _idempotency_store().abort(idempotency_scope)
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
        if not getattr(self, "_tracked_request", False):
            _REQUESTS.begin()
            self._tracked_request = True
        try:
            (dispatch or self._dispatch)()
        except ApiError as error:
            response_headers = {}
            if error.code == "queue_full":
                response_headers["Retry-After"] = "1"
            elif error.code == "maintenance":
                response_headers["Retry-After"] = str(self._maintenance_retry_after)
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
        finally:
            if self._tracked_request:
                self._tracked_request = False
                _REQUESTS.finish()
            if _SHUTDOWN_REQUESTED.is_set():
                self.close_connection = True

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
    loaded = settings.current()
    if loaded.errors:
        for error in loaded.errors:
            print(f"config error: {error.field}: {error.message}", file=sys.stderr)
        raise SystemExit(2)
    server = ThreadingHTTPServer(("0.0.0.0", loaded.values["APP_PORT"]), Handler)
    _reset_shutdown_tracking()
    previous_handlers = {
        sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    shutdown_requested_at: float | None = None

    def stop_accepting(signum: int, frame: object) -> None:
        nonlocal shutdown_requested_at
        if _SHUTDOWN_REQUESTED.is_set():
            return
        shutdown_requested_at = time.monotonic()
        _SHUTDOWN_REQUESTED.set()

        def shutdown_server() -> None:
            try:
                server.shutdown()
            except Exception:
                LOGGER.exception("HTTP server shutdown request failed")

        threading.Thread(
            target=shutdown_server, name="http-shutdown", daemon=True
        ).start()

    try:
        signal.signal(signal.SIGTERM, stop_accepting)
        signal.signal(signal.SIGINT, stop_accepting)
        server.serve_forever()
    finally:
        if _SHUTDOWN_REQUESTED.is_set():
            timeout = int(loaded.values["AGENT_QA_SHUTDOWN_TIMEOUT_SECONDS"])
            deadline = (shutdown_requested_at or time.monotonic()) + timeout
            remaining = max(0.0, deadline - time.monotonic())
            drained = _REQUESTS.wait(remaining)
            if not drained:
                print(
                    "forced shutdown after in-flight request timeout",
                    file=sys.stderr,
                )
            worker_timeout = min(5.0, max(0.0, deadline - time.monotonic()))
        else:
            worker_timeout = 5.0
        try:
            tenants.TENANTS.shutdown(timeout=worker_timeout)
        finally:
            try:
                server.server_close()
            finally:
                for sig, previous in previous_handlers.items():
                    signal.signal(sig, previous)
