"""Socket-free HTTP tenancy behavior checks."""

import io
import json
import unittest
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

from agent_qa import tenants
from agent_qa.audit import AuditLog
from agent_qa.context import RequestContext, clear_context, set_context
from agent_qa.errors import ApiError, envelope
from agent_qa.keys import KeyStore
from agent_qa.orders import OrderStore
from agent_qa.outbox import OutboxStore
from agent_qa.products import ProductStore
from agent_qa.routes import metrics
from agent_qa.server import Handler


class Headers(dict):
    def get_all(self, name, default=None):
        value = self.get(name)
        return [value] if value is not None else (default or [])


class Harness:
    def __init__(self, method, path, body=None, headers=None):
        self.command = method
        self.path = path
        self.request_id = "tenant-api-test"
        self.client_address = ("127.0.0.1", 12345)
        self._request_started = perf_counter()
        self._response_recorded = False
        self.headers = Headers(headers or {})
        if body is not None:
            encoded = json.dumps(body).encode("utf-8")
            self.headers["Content-Length"] = str(len(encoded))
            self.headers["Content-Type"] = "application/json"
        else:
            encoded = b""
        self.rfile = io.BytesIO(encoded)
        self.wfile = io.BytesIO()
        self.sent_headers = []
        self.responses = []

    def _json(self, status, body, headers=None):
        self.responses.append((status, body, headers or {}))
        Handler._json(self, status, body, headers)

    def send_response(self, status):
        self.sent_status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def _record_response(self, status):
        Handler._record_response(self, status)

    def _prepare_tenant(self, scoped):
        Handler._prepare_tenant(self, scoped)

    def _read_request_body(
        self, consumes=("application/json",), require_object=True, max_body_bytes=4096
    ):
        return Handler._read_request_body(
            self, consumes, require_object, max_body_bytes
        )

    def _method_not_allowed(self, routes):
        self._json(405, envelope("method_not_allowed", "Method not allowed"))


class TenantApiTests(unittest.TestCase):
    def setUp(self):
        self.registry = tenants.TenantRegistry(
            default_outbox=OutboxStore(start_dispatcher=False)
        )
        self.registry_patch = patch.object(tenants, "TENANTS", self.registry)
        self.registry_patch.start()
        self.addCleanup(self.registry_patch.stop)
        self.audit = AuditLog(30)
        self.audit_patch = patch("agent_qa.server.AUDIT_LOG", self.audit)
        self.audit_patch.start()
        self.addCleanup(self.audit_patch.stop)
        self.key_store = KeyStore("test-bootstrap")
        self.key_store_patch = patch("agent_qa.routes.auth.KEY_STORE", self.key_store)
        self.key_store_patch.start()
        self.addCleanup(self.key_store_patch.stop)
        self.log_patch = patch("agent_qa.server.write_access_log")
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        self.rate_patch = patch(
            "agent_qa.server.RATE_LIMITER.consume",
            return_value=SimpleNamespace(
                allowed=True, limit=10, remaining=9, reset_after=0, retry_after=0
            ),
        )
        self.rate_patch.start()
        self.addCleanup(self.rate_patch.stop)

    def tearDown(self):
        for name in self.registry.tenant_names():
            if name != "default":
                self.registry.delete(name)

    def request(
        self,
        method,
        path,
        body=None,
        *,
        tenant=None,
        allowed=None,
        idempotency_key=None,
        role="admin",
        authenticated=True,
    ):
        headers = {}
        if tenant is not None:
            headers["X-Tenant"] = tenant
        if body is not None:
            headers["X-API-Key"] = "test-key"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        request = Harness(method, path, body, headers)
        set_context(RequestContext(request_id=request.request_id))
        identity = (
            {
                "key_id": "test",
                "role": role,
                "label": "test",
                "tenants": allowed,
            }
            if authenticated
            else None
        )
        try:
            with patch("agent_qa.server.authenticate_api_key", return_value=identity):
                try:
                    Handler._dispatch(request)
                except ApiError as error:
                    request._json(
                        error.status,
                        envelope(error.code, error.message, error.details),
                    )
        finally:
            clear_context()
        status, response, response_headers = request.responses[0]
        wire_headers = dict(request.sent_headers)
        return status, response, response_headers, wire_headers

    def test_tenant_header_validation_echo_and_read_does_not_create(self):
        status, body, _, wire_headers = self.request("GET", "/orders", tenant="acme")
        self.assertEqual(status, 200)
        self.assertEqual(body["items"], [])
        self.assertEqual(wire_headers["X-Tenant"], "acme")
        self.assertEqual(self.registry.tenant_names(), ("default",))

        status, body, _, wire_headers = self.request("GET", "/orders", tenant="Bad")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_tenant")
        self.assertNotIn("X-Tenant", wire_headers)

        status, body, _, _ = self.request("GET", "/orders", tenant="a" * 25)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_tenant")

    def test_key_allowlist_blocks_creation_and_allowed_write_creates_tenant(self):
        status, body, _, wire_headers = self.request(
            "POST",
            "/orders",
            {"customer_id": "allowed-order", "total_cents": 500},
            tenant="blocked",
            allowed=["permitted"],
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")
        self.assertEqual(
            body["error"]["details"],
            [{"field": "tenant", "message": "Key is not allowed for this tenant"}],
        )
        self.assertEqual(wire_headers["X-Tenant"], "blocked")
        self.assertEqual(self.registry.tenant_names(), ("default",))
        denied_entries = self.audit.query(tenant="blocked")["items"]
        self.assertEqual(denied_entries[0]["outcome"], "denied")

        status, _, _, _ = self.request(
            "GET", "/orders", tenant="blocked", allowed=["permitted"], role="read"
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.registry.tenant_names(), ("default",))

        status, _, _, _ = self.request(
            "POST",
            "/orders",
            {"customer_id": "unauthorized", "total_cents": 500},
            tenant="unauthorized",
            authenticated=False,
        )
        self.assertEqual(status, 401)
        self.assertEqual(self.registry.tenant_names(), ("default",))

        status, body, _, wire_headers = self.request(
            "POST",
            "/orders",
            {"customer_id": "allowed-order", "total_cents": 500},
            tenant="permitted",
            allowed=["permitted"],
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["id"], 1)
        self.assertEqual(wire_headers["X-Tenant"], "permitted")
        self.assertIn("permitted", self.registry.tenant_names())

    def test_orders_products_cursors_and_idempotency_are_tenant_local(self):
        payload = {"customer_id": "cursor-owner", "total_cents": 500}
        first = self.request(
            "POST", "/orders", payload, tenant="alpha", idempotency_key="shared"
        )
        replay = self.request(
            "POST", "/orders", payload, tenant="alpha", idempotency_key="shared"
        )
        other = self.request(
            "POST", "/orders", payload, tenant="beta", idempotency_key="shared"
        )
        self.assertEqual((first[0], replay[0], other[0]), (201, 201, 201))
        self.assertEqual(first[1]["id"], other[1]["id"])
        self.assertEqual(replay[3]["Idempotent-Replay"], "true")
        self.assertNotIn("Idempotent-Replay", other[3])

        self.request(
            "POST",
            "/orders",
            {"customer_id": "cursor-owner-2", "total_cents": 600},
            tenant="alpha",
            idempotency_key="second",
        )
        _, page, _, _ = self.request(
            "GET", "/orders?limit=1&pagination=cursor", tenant="alpha"
        )
        self.assertTrue(page["next_cursor"])
        status, mismatch, _, _ = self.request(
            "GET",
            "/orders?limit=1&pagination=cursor&cursor=" + page["next_cursor"],
            tenant="beta",
        )
        self.assertEqual(status, 400)
        self.assertEqual(mismatch["error"]["code"], "cursor_mismatch")

        product = {
            "sku": "SKU-ALPHA",
            "name": "Alpha item",
            "category": "tools",
            "price_cents": 400,
            "stock": 2,
        }
        status, created, _, _ = self.request(
            "POST", "/products", product, tenant="alpha"
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["id"], 1)
        status, listing, _, _ = self.request("GET", "/products", tenant="beta")
        self.assertEqual(status, 200)
        self.assertEqual(listing["total"], 0)

    def test_tenant_limit_admin_errors_and_key_allowlist_validation(self):
        for index in range(9):
            self.registry.ensure(f"existing-{index}")
        status, body, _, _ = self.request(
            "POST",
            "/orders",
            {"customer_id": "limit-check", "total_cents": 500},
            tenant="eleventh",
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "tenant_limit")

        status, body, _, _ = self.request("DELETE", "/admin/tenants/default")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "default_tenant_protected")
        status, body, _, _ = self.request("DELETE", "/admin/tenants/missing")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "tenant_not_found")

        status, body, _, _ = self.request(
            "POST",
            "/admin/keys",
            {"role": "read", "label": "null-tenants", "tenants": None},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")
        status, created, _, _ = self.request(
            "POST",
            "/admin/keys",
            {"role": "read", "label": "limited", "tenants": ["blue"]},
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["tenants"], ["blue"])
        listing = self.request("GET", "/admin/keys")[1]
        self.assertEqual(listing["items"][-1]["tenants"], ["blue"])

    def test_admin_tenant_routes_ignore_header_and_purge(self):
        self.registry.ensure("remove-me")
        status, body, _, wire_headers = self.request(
            "GET", "/admin/tenants", tenant="INVALID"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["tenant"] for item in body["items"]], ["default", "remove-me"]
        )
        self.assertNotIn("X-Tenant", wire_headers)
        status, _, _, wire_headers = self.request(
            "DELETE", "/admin/tenants/remove-me", tenant="INVALID"
        )
        self.assertEqual(status, 204)
        self.assertNotIn("remove-me", self.registry.tenant_names())
        self.assertNotIn("X-Tenant", wire_headers)

    def test_metrics_include_all_tenants_and_use_patched_default_stores(self):
        default_orders = OrderStore()
        default_orders.create("default-order", total_cents=100)
        default_products = ProductStore()
        default_products.create(
            sku="SKU-DEFAULT",
            name="Default item",
            category="tools",
            price_cents=100,
            stock=1,
        )
        tenant_orders = self.registry.ensure("metric-tenant").orders
        tenant_orders.create("tenant-order", total_cents=200)
        with (
            patch("agent_qa.routes.ORDER_STORE", default_orders),
            patch("agent_qa.routes.PRODUCT_STORE", default_products),
        ):
            _, rendered, _ = metrics([])
        self.assertIn("agent_qa_orders 2\n", rendered)
        self.assertIn('agent_qa_tenant_orders{tenant="default"} 1', rendered)
        self.assertIn('agent_qa_tenant_orders{tenant="metric-tenant"} 1', rendered)
        self.assertIn("agent_qa_products 1\n", rendered)


if __name__ == "__main__":
    unittest.main()
