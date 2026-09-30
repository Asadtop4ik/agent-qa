"""HTTP server adapter for the route handlers."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

from agent_qa import config
from agent_qa.errors import ApiError, envelope
from agent_qa.routes import ROUTES


def _path_routes(path: str) -> list[dict[str, object]]:
    return [route for route in ROUTES if route["path"] == path]


def allowed_methods(path: str) -> str:
    routes = _path_routes(path)
    return ", ".join(dict.fromkeys(str(route["method"]) for route in routes))


class Handler(BaseHTTPRequestHandler):
    def _json(
        self, status: int, body: object, headers: dict[str, str] | None = None
    ) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)

    def _not_found(self) -> None:
        self._json(404, envelope("not_found", "Route not found"))

    def _internal_error(self) -> None:
        self._json(500, envelope("internal_error", "Internal server error"))

    def _handle_get(self) -> None:
        parsed = urlsplit(self.path)
        routes = _path_routes(parsed.path)
        route = next((item for item in routes if item["method"] == "GET"), None)
        if route is None:
            if routes:
                self._method_not_allowed(routes)
            else:
                self._not_found()
            return
        query = parse_qsl(parsed.query, keep_blank_values=True)
        status, body, headers = route["handler"](query)
        self._json(status, body, headers)

    def _handle_unsupported_method(self) -> None:
        try:
            routes = _path_routes(urlsplit(self.path).path)
            if not routes:
                self._not_found()
                return
            route = next(
                (item for item in routes if item["method"] == self.command), None
            )
            if route is None:
                self._method_not_allowed(routes)
                return
            parsed = urlsplit(self.path)
            result = route["handler"](parse_qsl(parsed.query, keep_blank_values=True))
            self._json(*result)
        except ApiError as error:
            self._json(error.status, envelope(error.code, error.message, error.details))
        except Exception:
            self._internal_error()

    def _method_not_allowed(self, routes: list[dict[str, object]]) -> None:
        self._json(
            405,
            envelope("method_not_allowed", "Method not allowed"),
            {"Allow": allowed_methods(str(routes[0]["path"]))},
        )

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            self._handle_get()
        except ApiError as error:
            self._json(error.status, envelope(error.code, error.message, error.details))
        except Exception:
            self._internal_error()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle_unsupported_method()

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle_unsupported_method()

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle_unsupported_method()

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle_unsupported_method()

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle_unsupported_method()

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle_unsupported_method()

    def log_message(self, fmt: str, *args: object) -> None:
        print("agent-qa: " + fmt % args)


def main() -> None:
    ThreadingHTTPServer(("0.0.0.0", config.port()), Handler).serve_forever()
