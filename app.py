"""Synthetic-only HTTP service for private agent QA workflows."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import platform
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


APP_DIR = Path(__file__).resolve().parent
FIXTURE_PATH = APP_DIR / "data" / "synthetic-customer.json"
GIT_SHA = os.environ.get("AGENT_QA_GIT_SHA", "unknown")


class Handler(BaseHTTPRequestHandler):
    _known_paths = {"/ready", "/fixture", "/version"}

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
        self.wfile.write(encoded)

    def _not_found(self) -> None:
        self._json(
            404,
            {"error": {"code": "not_found", "message": "Route not found"}},
        )

    def _internal_error(self) -> None:
        self._json(
            500,
            {
                "error": {
                    "code": "internal_error",
                    "message": "Internal server error",
                }
            },
        )

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            self._handle_get()
        except Exception:
            self._internal_error()

    def _handle_get(self) -> None:
        path = urlsplit(self.path).path
        if path == "/ready":
            self._json(200, {"status": "ready", "git_sha": GIT_SHA})
            return
        if path == "/version":
            self._json(
                200,
                {
                    "service": "agent-qa",
                    "git_sha": GIT_SHA,
                    "python_version": platform.python_version(),
                },
            )
            return
        if path == "/fixture":
            query = parse_qsl(urlsplit(self.path).query, keep_blank_values=True)
            field_values = [value for name, value in query if name == "fields"]
            invalid_param = next((name for name, _ in query if name != "fields"), None)
            if invalid_param is not None:
                self._invalid_query(
                    invalid_param, f"Unsupported query parameter: {invalid_param}"
                )
                return
            if len(field_values) > 1:
                self._invalid_query("fields", "The fields parameter may appear once")
                return
            with FIXTURE_PATH.open(encoding="utf-8") as fixture:
                data = json.load(fixture)
            if field_values:
                requested_fields = field_values[0].split(",")
                if any(not field for field in requested_fields):
                    self._invalid_query("fields", "Fields must not be empty")
                    return
                unknown_fields = [
                    field for field in requested_fields if field not in data
                ]
                if unknown_fields:
                    self._invalid_query("fields", f"Unknown field: {unknown_fields[0]}")
                    return
                data = {field: data[field] for field in dict.fromkeys(requested_fields)}
            self._json(200, data)
            return
        self._not_found()

    def _handle_unsupported_method(self) -> None:
        try:
            path = urlsplit(self.path).path
            if path not in self._known_paths:
                self._not_found()
                return
            self._json(
                405,
                {
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Method not allowed",
                    }
                },
                {"Allow": "GET"},
            )
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

    def _invalid_query(self, param: str, message: str) -> None:
        self._json(
            400,
            {
                "error": {
                    "code": "invalid_query",
                    "message": message,
                    "details": [{"param": param, "message": message}],
                }
            },
        )

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep logs concise and avoid echoing request payloads.
        print("agent-qa: " + fmt % args)


if __name__ == "__main__":
    port = int(os.environ.get("APP_PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
