"""Focused maintenance route and dispatch behavior tests."""

import io
import unittest
from types import SimpleNamespace
from time import perf_counter
from unittest.mock import patch

from agent_qa import server
from agent_qa.audit import AuditLog
from agent_qa.context import RequestContext, clear_context, set_context
from agent_qa.maintenance import MAINTENANCE
from agent_qa.openapi import build_openapi
from agent_qa.routes import ROUTES, get_maintenance, put_maintenance


class Headers(dict):
    def get_all(self, name, default=None):
        value = self.get(name)
        return [value] if value is not None else (default or [])


class Harness:
    def __init__(self, method, path, body=b""):
        self.command = method
        self.path = path
        self.request_id = "maintenance-test"
        self.headers = Headers()
        self.headers["Content-Length"] = str(len(body))
        self.headers["Content-Type"] = "application/json"
        self.headers["X-API-Key"] = "maintenance-test-key"
        self.rfile = io.BytesIO(body)
        self._request_started = perf_counter()
        self._response_recorded = False
        self._rate_headers = {}
        self._tenant_write_pin = None
        self.client_address = ("127.0.0.1", 12345)
        self.responses = []

    def _dispatch(self):
        server.Handler._dispatch(self)

    def _read_request_body(self, *args, **kwargs):
        return server.Handler._read_request_body(self, *args, **kwargs)

    def _json(self, status, body, headers=None):
        self.responses.append((status, body, headers or {}))
        with (
            patch("agent_qa.server.REGISTRY.record"),
            patch("agent_qa.server.write_access_log"),
        ):
            server.Handler._record_response(self, status)

    def _internal_error(self):
        self._json(500, {})


class MaintenanceApiTests(unittest.TestCase):
    def setUp(self):
        MAINTENANCE.update(False, retry_after_seconds=30)

    def tearDown(self):
        MAINTENANCE.update(False, retry_after_seconds=30)
        clear_context()

    def test_get_and_put_maintenance_state_validation(self):
        status, initial, _ = get_maintenance([])
        self.assertEqual(status, 200)
        self.assertEqual(
            initial,
            {
                "enabled": False,
                "message": None,
                "retry_after_seconds": 30,
                "since": None,
            },
        )

        status, enabled, _ = put_maintenance(
            [],
            payload={
                "enabled": True,
                "message": "Planned maintenance",
                "retry_after_seconds": 45,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(enabled["enabled"])
        self.assertEqual(enabled["message"], "Planned maintenance")
        self.assertEqual(enabled["retry_after_seconds"], 45)
        self.assertIsInstance(enabled["since"], str)

        for payload in (
            {"enabled": 1},
            {"enabled": True, "message": ""},
            {"enabled": True, "message": "x" * 201},
            {"enabled": False, "retry_after_seconds": 3601},
        ):
            with self.subTest(payload=payload), self.assertRaises(Exception) as error:
                put_maintenance([], payload=payload)
            self.assertEqual(error.exception.status, 400)

        _, disabled, _ = put_maintenance([], payload={"enabled": False})
        self.assertIsNone(disabled["message"])
        self.assertIsNone(disabled["since"])

    def test_maintenance_rejects_write_after_auth_before_body_or_handler(self):
        MAINTENANCE.update(True, "Back shortly", 17)
        context = RequestContext(request_id="maintenance-test")
        set_context(context)
        harness = Harness("POST", "/orders", b"invalid-json")
        audit = AuditLog(10)
        allowed = SimpleNamespace(allowed=True, limit=120, remaining=119, reset_after=1)
        with (
            patch(
                "agent_qa.server.authenticate_api_key",
                return_value={"key_id": "writer", "role": "write", "tenants": None},
            ),
            patch("agent_qa.server.RATE_LIMITER.consume", return_value=allowed),
            patch.object(
                MAINTENANCE,
                "snapshot",
                return_value={
                    "enabled": True,
                    "message": "Back shortly",
                    "retry_after_seconds": 17,
                    "since": "now",
                },
            ) as snapshot,
            patch("agent_qa.server.AUDIT_LOG", audit),
        ):
            server.Handler._handle(harness)
        snapshot.assert_called_once_with()
        self.assertEqual(harness.responses[0][0], 503)
        self.assertEqual(harness.responses[0][1]["error"]["code"], "maintenance")
        self.assertEqual(harness.responses[0][1]["error"]["message"], "Back shortly")
        self.assertEqual(harness.responses[0][2]["Retry-After"], "17")
        outcome = audit.query(tenant="default")["items"][0]["outcome"]
        self.assertEqual(outcome, "rejected")

    def test_auth_and_role_errors_precede_maintenance_and_admin_keys_are_blocked(self):
        MAINTENANCE.update(True, "Back shortly", 17)
        allowed = SimpleNamespace(allowed=True, limit=120, remaining=119, reset_after=1)
        cases = (
            ("POST", "/orders", None, 401),
            ("POST", "/orders", {"role": "read"}, 403),
            ("POST", "/orders", {"role": "admin"}, 503),
            ("GET", "/admin/maintenance", None, 401),
            ("GET", "/admin/maintenance", {"role": "read"}, 403),
        )
        for method, path, identity, expected in cases:
            harness = Harness(method, path, b"invalid-json")
            with (
                patch("agent_qa.server.authenticate_api_key", return_value=identity),
                patch("agent_qa.server.RATE_LIMITER.consume", return_value=allowed),
            ):
                server.Handler._handle(harness)
            self.assertEqual(harness.responses[0][0], expected)

    def test_maintenance_rejection_is_recorded_as_a_rejected_audit_outcome(self):
        audit = AuditLog(10)
        context = RequestContext(
            request_id="maintenance-audit-test", actor="writer", role="write"
        )
        set_context(context)
        handler = SimpleNamespace(
            _response_recorded=False,
            path="/orders",
            command="POST",
            request_id="maintenance-audit-test",
            _request_started=0.0,
            _audit_rejected=True,
        )
        with (
            patch("agent_qa.server.AUDIT_LOG", audit),
            patch("agent_qa.server.REGISTRY.record"),
            patch("agent_qa.server.write_access_log"),
        ):
            server.Handler._record_response(handler, 503)
        outcome = audit.query(tenant="default")["items"][0]["outcome"]
        self.assertEqual(outcome, "rejected")

    def test_admin_routes_remain_available_during_maintenance(self):
        MAINTENANCE.update(True)
        route = next(
            route
            for route in ROUTES
            if route["method"] == "GET" and route["path"] == "/admin/maintenance"
        )
        self.assertEqual(route["role"], "admin")
        self.assertEqual(get_maintenance([])[1]["enabled"], True)

        set_context(RequestContext(request_id="maintenance-admin-test"))
        admin_update = Harness("PUT", "/admin/maintenance", b'{"enabled":false}')
        ready = Harness("GET", "/ready")
        with patch(
            "agent_qa.server.authenticate_api_key",
            return_value={"key_id": "admin", "role": "admin", "tenants": None},
        ):
            server.Handler._handle(ready)
            server.Handler._handle(admin_update)
        self.assertEqual(admin_update.responses[0][0], 200)
        self.assertFalse(admin_update.responses[0][1]["enabled"])
        self.assertEqual(ready.responses[0][0], 200)

    def test_route_table_and_openapi_document_maintenance_contract(self):
        routes = {
            route["method"].lower(): route
            for route in ROUTES
            if route["path"] == "/admin/maintenance"
        }
        self.assertEqual(set(routes), {"get", "put"})
        self.assertEqual(routes["get"]["role"], "admin")
        self.assertEqual(routes["put"]["role"], "admin")

        spec = build_openapi(ROUTES, "maintenance-test")
        put = spec["paths"]["/admin/maintenance"]["put"]
        self.assertEqual(
            put["requestBody"]["content"]["application/json"]["schema"]["required"],
            ["enabled"],
        )
        order_write = spec["paths"]["/orders"]["post"]["responses"]["503"]
        self.assertIn("maintenance", order_write["description"])
        self.assertIn("Retry-After", order_write["headers"])


if __name__ == "__main__":
    unittest.main()
