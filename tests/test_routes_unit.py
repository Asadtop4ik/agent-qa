import json
import os
import subprocess
import sys
import tempfile
import unittest
from email.message import Message
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

from agent_qa.config import GIT_SHA
from agent_qa.errors import ApiError, envelope
from agent_qa.routes import ROUTES, fixture, health, ping, ready, status, version
from agent_qa.schemas import SCHEMAS


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

    def test_order_request_schema_identity(self):
        create = next(
            route
            for route in ROUTES
            if route["method"] == "POST" and route["path"] == "/orders"
        )
        update = next(
            route
            for route in ROUTES
            if route["method"] == "PATCH" and route["path"] == "/orders/{id}"
        )
        self.assertIs(create["request_schema"], SCHEMAS["CreateOrder"])
        self.assertIs(update["request_schema"], SCHEMAS["UpdateOrder"])

    def test_schema_routes_are_public_and_validate_raw_json(self):
        list_route = next(route for route in ROUTES if route["path"] == "/schemas")
        get_route = next(
            route for route in ROUTES if route["path"] == "/schemas/{name}"
        )
        validate_route = next(
            route for route in ROUTES if route["path"] == "/schemas/{name}/validate"
        )
        self.assertFalse(list_route["auth_required"])
        self.assertFalse(get_route["auth_required"])
        self.assertFalse(validate_route["auth_required"])
        self.assertFalse(validate_route["json_object_only"])
        self.assertEqual(list_route["handler"]([])[1]["items"], sorted(SCHEMAS))
        self.assertIs(
            get_route["handler"]([], {"name": "CreateOrder"})[1],
            SCHEMAS["CreateOrder"],
        )
        self.assertEqual(
            validate_route["handler"]([], {"name": "CreateOrder"}, None)[1]["valid"],
            False,
        )

    @staticmethod
    def make_dispatcher(path, body, method="POST", content_type="application/json"):
        from agent_qa.server import Handler

        headers = Message()
        headers["Content-Length"] = str(len(body))
        headers["Content-Type"] = content_type
        responses = []
        handler = object.__new__(Handler)
        handler.path = path
        handler.command = method
        handler.headers = headers
        handler.rfile = BytesIO(body)
        handler.request_id = "dispatch-test"
        handler._json = lambda *args: responses.append(args)
        return handler, responses

    def test_dispatch_schema_api_accepts_scalar_and_array_json(self):
        from agent_qa.server import Handler

        for raw in (b"null", b"[]"):
            handler, responses = self.make_dispatcher(
                "/schemas/CreateOrder/validate", raw
            )
            Handler._dispatch(handler)
            self.assertEqual(responses[0][0], 200)
            self.assertFalse(responses[0][1]["valid"])

        handler, _ = self.make_dispatcher("/schemas/DoesNotExist/validate", b"null")
        with self.assertRaises(ApiError) as error:
            Handler._dispatch(handler)
        self.assertEqual(error.exception.status, 404)
        self.assertEqual(error.exception.code, "schema_not_found")

    def test_dispatch_auth_and_json_errors_precede_validation(self):
        from agent_qa.server import Handler

        handler, responses = self.make_dispatcher("/orders", b"not-json")
        with patch("agent_qa.server.is_valid_api_key", return_value=False):
            Handler._dispatch(handler)
        self.assertEqual(responses[0][0], 401)

        handler, _ = self.make_dispatcher("/orders", b"{}")
        del handler.headers["Content-Length"]
        with patch("agent_qa.server.is_valid_api_key", return_value=True):
            with self.assertRaises(ApiError) as error:
                Handler._dispatch(handler)
        self.assertEqual(error.exception.status, 411)
        self.assertEqual(error.exception.code, "length_required")

        handler, _ = self.make_dispatcher("/orders", b" " * 4097)
        with patch("agent_qa.server.is_valid_api_key", return_value=True):
            with self.assertRaises(ApiError) as error:
                Handler._dispatch(handler)
        self.assertEqual(error.exception.status, 413)
        self.assertEqual(error.exception.code, "payload_too_large")

        handler, _ = self.make_dispatcher(
            "/orders", b"not-json", content_type="text/plain"
        )
        with patch("agent_qa.server.is_valid_api_key", return_value=True):
            with self.assertRaises(ApiError) as error:
                Handler._dispatch(handler)
        self.assertEqual(error.exception.status, 415)
        self.assertEqual(error.exception.code, "unsupported_media_type")

        for raw in (b"NaN", b"1e999"):
            handler, _ = self.make_dispatcher("/orders", raw)
            with patch("agent_qa.server.is_valid_api_key", return_value=True):
                with self.assertRaises(ApiError) as error:
                    Handler._dispatch(handler)
            self.assertEqual(error.exception.status, 400)
            self.assertEqual(error.exception.code, "invalid_json")

    def test_dispatch_validates_before_calling_handler(self):
        from agent_qa.server import Handler

        handler, _ = self.make_dispatcher("/dispatch-test", b"{}")
        route_handler = Mock(side_effect=AssertionError("handler should not run"))
        route = {
            "path": "/dispatch-test",
            "method": "POST",
            "handler": route_handler,
            "body": True,
            "auth_required": False,
            "request_schema": {
                "type": "object",
                "required": ["required"],
                "properties": {"required": {"type": "string"}},
            },
        }
        with patch("agent_qa.server.ROUTES", (route,)):
            with self.assertRaises(ApiError) as error:
                Handler._dispatch(handler)
        self.assertEqual(error.exception.code, "validation_error")
        self.assertEqual(
            error.exception.details,
            [{"field": "required", "message": "Required"}],
        )
        route_handler.assert_not_called()

    def test_dispatch_keeps_order_validation_details_for_legacy_payloads(self):
        from agent_qa.server import Handler

        cases = (
            (
                "POST",
                "/orders",
                {},
                [
                    {"field": "customer_id", "message": "Required"},
                    {"field": "total_cents", "message": "Required"},
                ],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": None},
                [
                    {"field": "customer_id", "message": "Must be a string"},
                    {"field": "total_cents", "message": "Required"},
                ],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": ""},
                [
                    {
                        "field": "customer_id",
                        "message": "Must contain 1 to 64 characters",
                    },
                    {"field": "total_cents", "message": "Required"},
                ],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": "  ", "total_cents": 1},
                [{"field": "customer_id", "message": "Must not be blank"}],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": "x" * 65, "total_cents": 1},
                [
                    {
                        "field": "customer_id",
                        "message": "Must contain 1 to 64 characters",
                    }
                ],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": "customer", "total_cents": True},
                [{"field": "total_cents", "message": "Must be an integer"}],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": "customer", "total_cents": -1},
                [
                    {
                        "field": "total_cents",
                        "message": "Must be between 0 and 100000000",
                    }
                ],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": "customer", "total_cents": 1, "status": "paid"},
                [{"field": "status", "message": "Unknown field"}],
            ),
            (
                "PATCH",
                "/orders/1",
                {},
                [{"field": "body", "message": "At least one field is required"}],
            ),
            (
                "PATCH",
                "/orders/1",
                {"status": False},
                [
                    {
                        "field": "status",
                        "message": "Must be one of: cancelled, new, paid, shipped",
                    }
                ],
            ),
            (
                "PATCH",
                "/orders/1",
                {"status": "unknown"},
                [
                    {
                        "field": "status",
                        "message": "Must be one of: cancelled, new, paid, shipped",
                    }
                ],
            ),
            (
                "PATCH",
                "/orders/1",
                {"total_cents": True},
                [{"field": "total_cents", "message": "Must be an integer"}],
            ),
            (
                "PATCH",
                "/orders/1",
                {"total_cents": -1},
                [
                    {
                        "field": "total_cents",
                        "message": "Must be between 0 and 100000000",
                    }
                ],
            ),
            (
                "PATCH",
                "/orders/1",
                {"total_cents": 1.5},
                [{"field": "total_cents", "message": "Must be an integer"}],
            ),
            (
                "PATCH",
                "/orders/1",
                {"unknown": 1},
                [{"field": "unknown", "message": "Unknown field"}],
            ),
            (
                "PATCH",
                "/orders/1",
                {"status": "bad", "unknown": 1},
                [
                    {
                        "field": "status",
                        "message": "Must be one of: cancelled, new, paid, shipped",
                    },
                    {"field": "unknown", "message": "Unknown field"},
                ],
            ),
        )
        for method, path, payload, expected in cases:
            with self.subTest(method=method, payload=payload):
                raw_body = json.dumps(payload).encode("utf-8")
                handler, _ = self.make_dispatcher(path, raw_body, method=method)
                with patch("agent_qa.server.is_valid_api_key", return_value=True):
                    with self.assertRaises(ApiError) as error:
                        Handler._dispatch(handler)
                self.assertEqual(error.exception.status, 400)
                self.assertEqual(error.exception.code, "validation_error")
                self.assertEqual(error.exception.details, expected)

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
