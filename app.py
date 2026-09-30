"""Synthetic-only HTTP service for private agent QA workflows."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import platform
from pathlib import Path
from urllib.parse import urlsplit


APP_DIR = Path(__file__).resolve().parent
FIXTURE_PATH = APP_DIR / "data" / "synthetic-customer.json"
GIT_SHA = os.environ.get("AGENT_QA_GIT_SHA", "unknown")


class Handler(BaseHTTPRequestHandler):
    ROUTES = {"/ready", "/fixture", "/version"}

    def _json(self, status: int, body: object, allow: str | None = None) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        if allow is not None:
            self.send_header("Allow", allow)
        self.end_headers()
        self.wfile.write(encoded)

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"error": {"code": code, "message": message}})

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
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
                with FIXTURE_PATH.open(encoding="utf-8") as fixture:
                    self._json(200, json.load(fixture))
                return
            self._error(404, "not_found", "Route not found")
        except Exception:
            self._error(500, "internal_error", "Internal server error")

    def _unsupported_method(self) -> None:
        try:
            path = urlsplit(self.path).path
            if path not in self.ROUTES:
                self._error(404, "not_found", "Route not found")
                return
            self._json(
                405,
                {
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Method not allowed",
                    }
                },
                allow="GET",
            )
        except Exception:
            self._error(500, "internal_error", "Internal server error")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._unsupported_method()

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._unsupported_method()

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._unsupported_method()

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._unsupported_method()

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep logs concise and avoid echoing request payloads.
        print("agent-qa: " + fmt % args)


if __name__ == "__main__":
    port = int(os.environ.get("APP_PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
