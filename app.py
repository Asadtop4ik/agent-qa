"""Synthetic-only HTTP service for private agent QA workflows."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
FIXTURE_PATH = APP_DIR / "data" / "synthetic-customer.json"
GIT_SHA = os.environ.get("AGENT_QA_GIT_SHA", "unknown")


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, body: object) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/ready":
            self._json(200, {"status": "ready", "git_sha": GIT_SHA})
            return
        if self.path == "/fixture":
            with FIXTURE_PATH.open(encoding="utf-8") as fixture:
                self._json(200, json.load(fixture))
            return
        self._json(404, {"error": "not found"})

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep logs concise and avoid echoing request payloads.
        print("agent-qa: " + fmt % args)


if __name__ == "__main__":
    port = int(os.environ.get("APP_PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
