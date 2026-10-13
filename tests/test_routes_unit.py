import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_qa.config import GIT_SHA
from agent_qa.errors import ApiError, envelope
from agent_qa.routes import ROUTES, fixture, health, ping, ready, status, version
from agent_qa.server import Handler


def invoke(method, path, raw_body, api_key="qa-synthetic-key"):
    headers = {
        "Content-Length": str(len(raw_body)),
        "Content-Type": "application/json",
    }
    if api_key is not None:
        headers["X-API-Key"] = api_key
    response = {}
    handler = object.__new__(Handler)
    handler.command = method
    handler.path = path
    handler.headers = headers
    handler.rfile = io.BytesIO(raw_body)
    handler.request_id = "unit-test"
    handler._json = lambda status, body, extra=None: response.update(
        status=status, body=body, headers=extra or {}
    )
    handler._handle()
    return response


class RouteUnitTests(unittest.TestCase):
    def test_health_handler_and_route(self):
        code, body, headers = health([])
        self.assertEqual(code, 200)
        self.assertEqual(body, {"status": "ok"})
        self.assertEqual(headers, {})

        route = next(route for route in ROUTES if route["path"] == "/health")
        self.assertEqual(route["method"], "GET")
        self.assertIs(route["handler"], health)
        self.assertEqual(route["responses"], ["200"])

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

    def test_dispatch_authenticates_then_validates_before_handler(self):
        encoded = b"{}"
        unauthenticated = invoke("POST", "/orders", encoded, api_key=None)
        self.assertEqual(unauthenticated["status"], 401)
        self.assertEqual(unauthenticated["body"]["error"]["code"], "unauthorized")

        route = next(
            route
            for route in ROUTES
            if route["method"] == "POST" and route["path"] == "/orders"
        )
        original_handler = route["handler"]
        calls = []
        route["handler"] = lambda *args: calls.append(args)
        try:
            validated = invoke("POST", "/orders", encoded)
        finally:
            route["handler"] = original_handler
        self.assertEqual(validated["status"], 400)
        self.assertEqual(validated["body"]["error"]["code"], "validation_error")
        self.assertEqual(calls, [])

    def test_schema_validation_api_handles_valid_invalid_and_unknown_schemas(self):
        valid = invoke(
            "POST",
            "/schemas/CreateOrder/validate",
            b'{"customer_id":"abc","total_cents":1}',
            api_key=None,
        )
        self.assertEqual(valid["status"], 200)
        self.assertEqual(valid["body"], {"valid": True, "errors": []})

        for raw_body in (b"null", b"[]", b'"not an object"'):
            with self.subTest(raw_body=raw_body):
                response = invoke(
                    "POST",
                    "/schemas/CreateOrder/validate",
                    raw_body,
                    api_key=None,
                )
                self.assertEqual(response["status"], 200)
                self.assertEqual(response["body"]["valid"], False)
                self.assertEqual(
                    response["body"]["errors"][0],
                    {"field": "body", "message": "Must be a JSON object"},
                )

        missing = invoke("POST", "/schemas/missing/validate", b"{}", api_key=None)
        self.assertEqual(missing["status"], 404)
        self.assertEqual(missing["body"]["error"]["code"], "schema_not_found")

    def test_body_parser_bounds_oversize_and_rejects_deep_json(self):
        huge_integer = b'{"customer_id":"abc","total_cents":' + b"9" * 4000 + b"}"
        bounded = invoke("POST", "/orders", huge_integer)
        self.assertEqual(bounded["status"], 400)
        self.assertEqual(bounded["body"]["error"]["code"], "validation_error")

        oversized = invoke(
            "POST", "/schemas/CreateOrder/validate", b"0" * 5000, api_key=None
        )
        self.assertEqual(oversized["status"], 413)
        self.assertEqual(oversized["body"]["error"]["code"], "payload_too_large")

        deeply_nested = b"[" * 1100 + b"0" + b"]" * 1100
        invalid = invoke(
            "POST", "/schemas/CreateOrder/validate", deeply_nested, api_key=None
        )
        self.assertEqual(invalid["status"], 400)
        self.assertEqual(invalid["body"]["error"]["code"], "invalid_json")

        overflow = invoke(
            "POST", "/schemas/CreateOrder/validate", b"1e999", api_key=None
        )
        self.assertEqual(overflow["status"], 400)
        self.assertEqual(overflow["body"]["error"]["code"], "invalid_json")

        huge_id = invoke("GET", "/orders/" + "9" * 5000, b"")
        self.assertEqual(huge_id["status"], 404)
        self.assertEqual(huge_id["body"]["error"]["code"], "order_not_found")

    def test_order_api_keeps_legacy_validation_details(self):
        cases = (
            (
                "POST",
                "/orders",
                {},
                [("customer_id", "Required"), ("total_cents", "Required")],
            ),
            ("POST", "/orders", {"total_cents": 1}, [("customer_id", "Required")]),
            ("POST", "/orders", {"customer_id": "abc"}, [("total_cents", "Required")]),
            (
                "POST",
                "/orders",
                {"customer_id": 1, "total_cents": True},
                [
                    ("customer_id", "Must be a string"),
                    ("total_cents", "Must be an integer"),
                ],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": "x" * 65, "total_cents": 1},
                [("customer_id", "Must contain 1 to 64 characters")],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": "   ", "total_cents": 1},
                [("customer_id", "Must not be blank")],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": "abc", "total_cents": -1},
                [("total_cents", "Must be between 0 and 100000000")],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": "abc", "total_cents": 1, "z": 1},
                [("z", "Unknown field")],
            ),
            ("PATCH", "/orders/1", {}, [("body", "At least one field is required")]),
            ("PATCH", "/orders/1", {"z": 1}, [("z", "Unknown field")]),
            (
                "PATCH",
                "/orders/1",
                {"total_cents": True},
                [("total_cents", "Must be an integer")],
            ),
            (
                "PATCH",
                "/orders/1",
                {"total_cents": 100000001},
                [("total_cents", "Must be between 0 and 100000000")],
            ),
            (
                "PATCH",
                "/orders/1",
                {"status": "unknown"},
                [("status", "Must be one of: cancelled, new, paid, shipped")],
            ),
            (
                "PATCH",
                "/orders/1",
                {"status": 2},
                [("status", "Must be one of: cancelled, new, paid, shipped")],
            ),
            (
                "PATCH",
                "/orders/1",
                {"status": "new", "x": 1},
                [("x", "Unknown field")],
            ),
        )
        self.assertEqual(len(cases), 15)
        for method, path, payload, expected in cases:
            with self.subTest(method=method, payload=payload):
                encoded = json.dumps(payload).encode("utf-8")
                response = invoke(method, path, encoded)
                self.assertEqual(response["status"], 400)
                self.assertEqual(response["body"]["error"]["code"], "validation_error")
                self.assertEqual(
                    response["body"]["error"]["details"],
                    [
                        {"field": field, "message": message}
                        for field, message in expected
                    ],
                )

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
