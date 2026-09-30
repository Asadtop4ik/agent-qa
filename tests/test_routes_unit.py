import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_qa.config import GIT_SHA
from agent_qa.errors import ApiError, envelope
from agent_qa.routes import ROUTES, fixture, ping, ready, status, version


class RouteUnitTests(unittest.TestCase):
    def test_ping_handler(self):
        status, body, headers = ping([])
        self.assertEqual(status, 200)
        self.assertEqual(body, {"pong": True})
        self.assertEqual(headers, {})

    def test_ready_handler(self):
        status, body, headers = ready([])
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ready", "git_sha": GIT_SHA})
        self.assertEqual(headers, {})

    def test_about_handler(self):
        route = next(route for route in ROUTES if route["path"] == "/about")
        status, body, headers = route["handler"]([])
        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            {"service": "agent-qa", "git_sha": GIT_SHA, "environment": "qa"},
        )
        self.assertEqual(headers, {"X-Service": "agent-qa"})

    def test_status_handler_with_valid_fixture(self):
        code, body, headers = status([])
        self.assertEqual(code, 200)
        self.assertEqual(
            body,
            {"status": "ok", "service": "agent-qa", "checks": {"fixture": True}},
        )
        self.assertEqual(headers, {})

    def test_status_handler_with_invalid_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture_path = Path(directory) / "invalid.json"
            fixture_path.write_text("{invalid json", encoding="utf-8")
            with patch("agent_qa.routes.FIXTURE_PATH", fixture_path):
                code, body, headers = status([])

        self.assertEqual(code, 200)
        self.assertEqual(
            body,
            {
                "status": "degraded",
                "service": "agent-qa",
                "checks": {"fixture": False},
            },
        )
        self.assertEqual(headers, {})

    def test_fixture_handler_and_projection(self):
        status, body, headers = fixture([])
        self.assertEqual(status, 200)
        self.assertEqual(body["record_type"], "synthetic_customer_fixture")
        self.assertEqual(headers, {})

        status, body, _ = fixture([("fields", "name,plan")])
        self.assertEqual(status, 200)
        self.assertEqual(body, {"name": "Example QA Customer", "plan": "sandbox"})

    def test_fixture_query_validation(self):
        invalid_queries = (
            ([("fields", "")], "Fields must not be empty"),
            ([("fields", "name,,plan")], "Fields must not be empty"),
            ([("fields", "nope")], "Unknown field: nope"),
            (
                [("fields", "name"), ("fields", "plan")],
                "The fields parameter may appear once",
            ),
            ([("x", "1")], "Unsupported query parameter: x"),
        )
        for query, message in invalid_queries:
            with self.subTest(query=query), self.assertRaises(ApiError) as error:
                fixture(query)
            self.assertEqual(error.exception.status, 400)
            self.assertEqual(error.exception.code, "invalid_query")
            self.assertEqual(error.exception.message, message)
            self.assertEqual(
                error.exception.details,
                [
                    {
                        "param": "fields" if query[0][0] == "fields" else "x",
                        "message": message,
                    }
                ],
            )

    def test_version_handler(self):
        status, body, headers = version([])
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "agent-qa")
        self.assertEqual(body["git_sha"], GIT_SHA)
        self.assertTrue(body["python_version"])
        self.assertEqual(headers, {})

    def test_api_error_envelope(self):
        message = "Fields must not be empty"
        details = [{"param": "fields", "message": message}]
        error = ApiError(400, "invalid_query", message, details)
        self.assertEqual(
            envelope(error.code, error.message, error.details),
            {
                "error": {
                    "code": "invalid_query",
                    "message": "Fields must not be empty",
                    "details": [
                        {"param": "fields", "message": "Fields must not be empty"}
                    ],
                }
            },
        )

    def test_allow_methods_come_from_routes(self):
        from agent_qa.server import allowed_methods

        paths = {route["path"] for route in ROUTES}
        for path in paths:
            with self.subTest(path=path):
                methods = dict.fromkeys(
                    route["method"] for route in ROUTES if route["path"] == path
                )
                self.assertEqual(allowed_methods(path), ", ".join(methods))

    def test_server_import_does_not_bind_a_port_or_write_files(self):
        code = """
import builtins
import socket

def fail(*args, **kwargs):
    raise AssertionError('unexpected import side effect')

socket.socket.bind = fail
builtins.open = fail
import agent_qa.server
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_app_entry_point_is_at_most_twenty_lines(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        self.assertLessEqual(len(app_path.read_text(encoding="utf-8").splitlines()), 20)


if __name__ == "__main__":
    unittest.main()
