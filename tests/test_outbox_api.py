"""Network-free route integration tests for webhook outbox APIs."""

import io
import json
import unittest
from time import perf_counter
from unittest.mock import patch

from agent_qa.audit import AuditLog
from agent_qa.context import RequestContext, clear_context, set_context
from agent_qa.idempotency import IdempotencyStore
from agent_qa.keys import KeyStore
from agent_qa.orders import OrderStore
from agent_qa.outbox import OutboxStore
from agent_qa.products import ProductStore
from agent_qa.routes import (
    ROUTES,
    adjust_product_stock,
    create_order,
    create_orders_bulk,
    get_product,
    patch_product,
)
from agent_qa.server import Handler, _path_routes


class Headers(dict):
    def get_all(self, name, default=None):
        value = self.get(name)
        return [value] if value is not None else (default or [])


class Harness:
    def __init__(self, method, path, body=b"", key=None, idempotency_key=None):
        self.command = method
        self.path = path
        self.request_id = "outbox-api-test"
        self._request_started = perf_counter()
        self._response_recorded = False
        self.headers = Headers()
        if body is not None:
            self.headers["Content-Length"] = str(len(body))
            self.headers["Content-Type"] = "application/json"
        if key is not None:
            self.headers["X-API-Key"] = key
        if idempotency_key is not None:
            self.headers["Idempotency-Key"] = idempotency_key
        self.rfile = io.BytesIO(body or b"")
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

    def _read_json_body(self, require_object=True, max_body_bytes=4096):
        return Handler._read_json_body(self, require_object, max_body_bytes)

    def _record_response(self, status):
        Handler._record_response(self, status)

    def _dispatch(self):
        Handler._dispatch(self)

    def _internal_error(self):
        Handler._internal_error(self)


class OutboxApiTests(unittest.TestCase):
    def test_routes_declare_roles_and_literal_dispatcher_paths_win(self):
        roles = {(route["method"], route["path"]): route["role"] for route in ROUTES}
        self.assertEqual(roles["GET", "/webhooks"], "read")
        self.assertEqual(roles["POST", "/webhooks"], "write")
        self.assertEqual(roles["GET", "/outbox"], "read")
        self.assertEqual(roles["POST", "/outbox/process"], "admin")
        self.assertEqual(roles["GET", "/outbox/dispatcher"], "admin")
        self.assertEqual(
            [
                (route["path"], params)
                for route, params in _path_routes("/outbox/process")
            ],
            [("/outbox/process", {})],
        )
        self.assertEqual(
            [
                (route["path"], params)
                for route, params in _path_routes("/outbox/dispatcher")
            ],
            [("/outbox/dispatcher", {}), ("/outbox/dispatcher", {})],
        )

    def test_create_webhook_response_and_audit_never_include_secret(self):
        keys = KeyStore("outbox-bootstrap")
        key = keys.create("write", "writer")["key"]
        audit = AuditLog(10)
        secret = "never-return-this-secret"
        body = json.dumps(
            {
                "url": "https://hooks.example.invalid/x",
                "events": ["order.*"],
                "secret": secret,
            }
        ).encode()
        harness = Harness("POST", "/webhooks", body, key)
        safe_webhook = {
            "id": 1,
            "url": "https://hooks.example.invalid/x",
            "events": ["order.*"],
            "active": True,
            "max_attempts": 5,
            "backoff_base_ms": 1000,
            "secret_set": True,
            "created_at": "2026-10-01T00:00:00Z",
        }
        with (
            patch("agent_qa.auth.KEY_STORE", keys),
            patch("agent_qa.routes.OUTBOX.create_webhook", return_value=safe_webhook),
            patch("agent_qa.server.AUDIT_LOG", audit),
            patch("agent_qa.server.write_access_log"),
        ):
            set_context(RequestContext(request_id=harness.request_id))
            try:
                Handler._dispatch(harness)
            finally:
                clear_context()
        self.assertEqual(harness.responses[0][0], 201)
        self.assertNotIn(secret, json.dumps(harness.responses[0][1]))
        self.assertNotIn(secret, json.dumps(audit.query()))

    def test_order_events_are_emitted_from_successful_committed_results_only(self):
        created = {"id": 7, "customer_id": "customer", "total_cents": 100}
        with (
            patch("agent_qa.routes.FulfillmentService") as service,
            patch("agent_qa.routes.OUTBOX.emit") as emit,
        ):
            service.return_value.create.return_value = {**created, "version": 2}
            status, body, _ = create_order(
                [], payload={"customer_id": "customer", "total_cents": 100}
            )
            self.assertEqual(status, 201)
            self.assertEqual(body, created)
            emit.assert_called_once_with("order.created", created)

        bulk_results = {
            "results": [
                {"index": 0, "status": 424, "error": {"code": "rolled_back"}},
                {"index": 1, "status": 424, "error": {"code": "rolled_back"}},
            ],
            "summary": {"total": 2, "succeeded": 0, "failed": 2},
        }
        with (
            patch("agent_qa.routes.FulfillmentService") as service,
            patch("agent_qa.routes.OUTBOX.emit") as emit,
        ):
            service.return_value.create_bulk.return_value = (422, bulk_results)
            status, _, _ = create_orders_bulk([], payload={"items": [], "atomic": True})
            self.assertEqual(status, 422)
            emit.assert_not_called()

        partial_results = {
            "results": [
                {"index": 0, "status": 201, "data": created},
                {"index": 1, "status": 400, "error": {"code": "validation_error"}},
            ],
            "summary": {"total": 2, "succeeded": 1, "failed": 1},
        }
        with (
            patch("agent_qa.routes.FulfillmentService") as service,
            patch("agent_qa.routes.OUTBOX.emit") as emit,
        ):
            service.return_value.create_bulk.return_value = (207, partial_results)
            status, _, _ = create_orders_bulk(
                [], payload={"items": [{}, {}], "atomic": False}
            )
            self.assertEqual(status, 207)
            emit.assert_called_once_with("order.created", created)

    def test_product_patch_emits_updated_and_conditional_read_emits_nothing(self):
        product = {"id": 4, "name": "New name", "version": 2}
        with (
            patch(
                "agent_qa.routes.validate_product_patch",
                return_value={"name": "New name"},
            ),
            patch("agent_qa.routes.PRODUCT_STORE.get", return_value={"id": 4}),
            patch("agent_qa.routes.PRODUCT_STORE.update", return_value=product.copy()),
            patch("agent_qa.routes.OUTBOX.emit") as emit,
        ):
            status, body, _ = patch_product(
                [], {"id": "4"}, payload={"name": "New name"}
            )
            self.assertEqual(status, 200)
            self.assertEqual(body, {"id": 4, "name": "New name"})
            emit.assert_called_once_with("product.updated", body)

        with (
            patch(
                "agent_qa.routes.PRODUCT_STORE.get",
                return_value={"id": 4, "name": "New name", "version": 2},
            ),
            patch("agent_qa.routes._if_none_match", return_value=True),
            patch("agent_qa.routes.OUTBOX.emit") as emit,
        ):
            status, body, _ = get_product([], {"id": "4"}, request_headers={})
            self.assertEqual(status, 304)
            self.assertIsNone(body)
            emit.assert_not_called()

    def test_stock_adjustment_emits_a_product_update(self):
        adjusted = {"id": 4, "name": "Product", "stock": 6, "version": 3}
        with (
            patch(
                "agent_qa.routes.validate_adjust_stock",
                return_value={"delta": 1},
            ),
            patch("agent_qa.routes.PRODUCT_STORE.get", return_value={"id": 4}),
            patch("agent_qa.routes.PRODUCT_STORE.adjust_stock", return_value=adjusted),
            patch("agent_qa.routes.OUTBOX.emit") as emit,
        ):
            status, body, _ = adjust_product_stock(
                [], {"id": "4"}, payload={"delta": 1}
            )
            self.assertEqual(status, 200)
            self.assertEqual(body, {"id": 4, "name": "Product", "stock": 6})
            emit.assert_called_once_with("product.updated", body)

    def test_idempotent_order_replay_does_not_emit_another_event(self):
        keys = KeyStore("outbox-replay-bootstrap")
        key = keys.create("write", "writer")["key"]
        request_body = json.dumps(
            {"customer_id": "customer", "total_cents": 175}
        ).encode()
        replay_key = "outbox-replay-api-test"
        first = Harness("POST", "/orders", request_body, key, replay_key)
        second = Harness("POST", "/orders", request_body, key, replay_key)
        with (
            patch("agent_qa.auth.KEY_STORE", keys),
            patch("agent_qa.server.write_access_log"),
            patch("agent_qa.routes.OUTBOX.emit") as emit,
            patch("agent_qa.routes.FulfillmentService") as service,
        ):
            service.return_value.create.return_value = {
                "id": 19,
                "customer_id": "customer",
                "total_cents": 175,
                "version": 1,
            }
            for harness in (first, second):
                set_context(RequestContext(request_id=harness.request_id))
                try:
                    Handler._dispatch(harness)
                finally:
                    clear_context()
        self.assertEqual(first.responses[0][0], 201)
        self.assertEqual(second.responses[0][0], 201)
        self.assertEqual(second.responses[0][1], first.responses[0][1])
        self.assertIn(("Idempotent-Replay", "true"), second.sent_headers)
        service.return_value.create.assert_called_once()
        emit.assert_called_once_with(
            "order.created",
            {"id": 19, "customer_id": "customer", "total_cents": 175},
        )


class RealOutboxApiTests(unittest.TestCase):
    def setUp(self):
        self.keys = KeyStore("outbox-flow-bootstrap")
        self.credentials = {
            role: self.keys.create(role, role)["key"]
            for role in ("read", "write", "admin")
        }
        self.outbox = OutboxStore(clock=lambda: 1_800_000_000, start_dispatcher=False)
        self.audit = AuditLog(100)
        self.patchers = [
            patch("agent_qa.auth.KEY_STORE", self.keys),
            patch("agent_qa.routes.OUTBOX", self.outbox),
            patch("agent_qa.routes.ORDER_STORE", OrderStore()),
            patch("agent_qa.routes.PRODUCT_STORE", ProductStore()),
            patch("agent_qa.routes.AUDIT_LOG", self.audit),
            patch("agent_qa.server.AUDIT_LOG", self.audit),
            patch("agent_qa.server.IDEMPOTENCY_STORE", IdempotencyStore()),
            patch("agent_qa.server.write_access_log"),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.outbox.stop()
        for patcher in reversed(self.patchers):
            patcher.stop()

    def request(
        self,
        method,
        path,
        payload=None,
        *,
        role="write",
        raw_body=None,
        headers=None,
    ):
        body = raw_body
        if body is None and payload is not None:
            body = json.dumps(payload).encode()
        request_headers = dict(headers or {})
        harness = Harness(
            method,
            path,
            body,
            self.credentials.get(role),
            request_headers.get("Idempotency-Key"),
        )
        for name, value in request_headers.items():
            harness.headers[name] = value
        set_context(RequestContext(request_id=f"outbox-flow-{method}-{path}"))
        try:
            Handler._handle(harness)
        finally:
            clear_context()
        return harness.responses[0]

    @staticmethod
    def product_payload(sku="FLOW-1"):
        return {
            "sku": sku,
            "name": "Flow product",
            "category": "example",
            "price_cents": 250,
            "stock": 10,
        }

    def create_webhook(self, url="https://ok.invalid/hook", events=None, **options):
        payload = {
            "url": url,
            "events": events or ["*"],
            "secret": "flow-secret",
            **options,
        }
        return self.request("POST", "/webhooks", payload)

    def test_real_crud_and_order_product_mutations_emit_only_committed_events(self):
        status, webhook, _ = self.create_webhook()
        self.assertEqual(status, 201)
        self.assertTrue(webhook["secret_set"])
        self.assertNotIn("secret", webhook)

        status, listing, _ = self.request("GET", "/webhooks", role="read")
        self.assertEqual(status, 200)
        self.assertEqual(listing["items"][0], webhook)
        status, fetched, _ = self.request("GET", "/webhooks/1", role="read")
        self.assertEqual(status, 200)
        self.assertEqual(fetched, webhook)
        status, patched, _ = self.request("PATCH", "/webhooks/1", {"active": False})
        self.assertEqual(status, 200)
        self.assertFalse(patched["active"])
        status, missing, _ = self.request(
            "POST",
            "/webhooks",
            {"url": "https://ok.invalid", "events": ["*"], "secret": "secret"},
            role="read",
        )
        self.assertEqual(status, 403)
        self.assertEqual(missing["error"]["code"], "forbidden")

        # An inactive subscription receives no records; activating it permits matches.
        status, created, _ = self.request("POST", "/products", self.product_payload())
        self.assertEqual(status, 201)
        status, outbox_list, _ = self.request("GET", "/outbox", role="read")
        self.assertEqual(status, 200)
        self.assertEqual(outbox_list["total"], 0)
        self.request("PATCH", "/webhooks/1", {"active": True})
        status, created, _ = self.request(
            "POST", "/products", self.product_payload("FLOW-2")
        )
        self.assertEqual(status, 201)
        status, entries, _ = self.request("GET", "/outbox", role="read")
        self.assertEqual(status, 200)
        self.assertEqual(entries["total"], 1)
        self.assertEqual(entries["items"][0]["event_type"], "product.created")

        # A reservation mutates stock inside order commit but emits no product event.
        status, order, _ = self.request(
            "POST",
            "/orders",
            {
                "customer_id": "basket",
                "items": [{"product_id": created["id"], "quantity": 2}],
            },
        )
        self.assertEqual(status, 201)
        status, entries, _ = self.request("GET", "/outbox", role="read")
        self.assertEqual(
            [entry["event_type"] for entry in entries["items"]],
            ["product.created", "order.created"],
        )
        status, updated, _ = self.request(
            "PATCH", f"/orders/{order['id']}", {"status": "cancelled"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["status"], "cancelled")
        status, entries, _ = self.request("GET", "/outbox", role="read")
        self.assertEqual(entries["items"][-1]["event_type"], "order.updated")

        status, legacy_order, _ = self.request(
            "POST", "/orders", {"customer_id": "legacy", "total_cents": 250}
        )
        self.assertEqual(status, 201)
        status, _, _ = self.request("DELETE", f"/orders/{legacy_order['id']}")
        self.assertEqual(status, 204)

        self.request("PATCH", f"/products/{created['id']}", {"name": "Updated"})
        self.request("POST", f"/products/{created['id']}/adjust-stock", {"delta": 1})
        status, _, _ = self.request("DELETE", f"/products/{created['id']}")
        self.assertEqual(status, 204)
        status, entries, _ = self.request("GET", "/outbox", role="read")
        types = [entry["event_type"] for entry in entries["items"]]
        self.assertCountEqual(
            types,
            [
                "product.created",
                "order.created",
                "order.updated",
                "order.created",
                "order.deleted",
                "product.updated",
                "product.updated",
                "product.deleted",
            ],
        )
        self.assertEqual(
            entries["items"][4]["payload"]["data"], {"id": legacy_order["id"]}
        )
        self.assertEqual(entries["items"][-1]["payload"]["data"], {"id": created["id"]})

        status, temporary, _ = self.create_webhook(events=["product.created"])
        self.assertEqual(status, 201)
        self.request("POST", "/products", self.product_payload("FLOW-3"))
        status, before_delete, _ = self.request("GET", "/outbox", role="read")
        pending = next(
            item
            for item in before_delete["items"]
            if item["webhook_id"] == temporary["id"]
        )
        status, _, _ = self.request("DELETE", f"/webhooks/{temporary['id']}")
        self.assertEqual(status, 204)
        status, failed_delivery, _ = self.request(
            "GET", f"/outbox/{pending['id']}", role="read"
        )
        self.assertEqual(failed_delivery["status"], "failed")
        status, missing, _ = self.request(
            "GET", f"/webhooks/{temporary['id']}", role="read"
        )
        self.assertEqual(status, 404)
        self.assertEqual(missing["error"]["code"], "webhook_not_found")

    def test_real_validation_bulk_replay_and_deliveries(self):
        for invalid in (
            {
                "url": "https://example.com/hook",
                "events": ["*"],
                "secret": "valid-secret",
            },
            {
                "url": "https://ok.invalid",
                "events": ["unknown"],
                "secret": "valid-secret",
            },
            {"url": "https://ok.invalid", "events": ["*"], "secret": "short"},
            {
                "url": "https://ok.invalid",
                "events": ["*"],
                "secret": "valid-secret",
                "max_attempts": 10**80,
            },
        ):
            status, body, _ = self.request("POST", "/webhooks", invalid)
            self.assertEqual(status, 400)
            self.assertNotIn("valid-secret", json.dumps(body))
        deep_body = (
            b'{"url":"https://ok.invalid","events":["*"],"secret":"valid-secret","x":'
            + b"[" * 34
            + b"0"
            + b"]" * 34
            + b"}"
        )
        status, body, _ = self.request("POST", "/webhooks", raw_body=deep_body)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")
        huge_id = "9" * 5000
        status, body, _ = self.request("GET", f"/outbox/{huge_id}", role="read")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_id")
        status, body, _ = self.request(
            "GET", "/outbox?limit=100000000000000000000", role="read"
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_query")

        self.create_webhook()
        payload = self.product_payload()
        status, product, _ = self.request(
            "POST", "/products", payload, headers={"Idempotency-Key": "product-replay"}
        )
        self.assertEqual(status, 201)
        status, replay, headers = self.request(
            "POST", "/products", payload, headers={"Idempotency-Key": "product-replay"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(replay, product)
        self.assertEqual(headers["Idempotent-Replay"], "true")
        self.assertEqual(self.outbox.list_outbox([])["total"], 1)

        status, _, _ = self.request(
            "POST",
            "/orders/bulk",
            {
                "items": [
                    {"customer_id": "rollback", "total_cents": 100},
                    {},
                ],
                "atomic": True,
            },
        )
        self.assertEqual(status, 422)
        self.assertEqual(self.outbox.list_outbox([])["total"], 1)
        status, partial, _ = self.request(
            "POST",
            "/orders/bulk",
            {
                "items": [
                    {"customer_id": "partial", "total_cents": 100},
                    {},
                ]
            },
        )
        self.assertEqual(status, 207)
        self.assertEqual(partial["summary"]["succeeded"], 1)
        self.assertEqual(self.outbox.list_outbox([])["total"], 2)

        status, failure, _ = self.request(
            "POST", "/orders", {"customer_id": "", "total_cents": 100}
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.outbox.list_outbox([])["total"], 2)

        status, processed, _ = self.request(
            "POST",
            "/outbox/process",
            {"ignore_schedule": True, "max": 100},
            role="admin",
        )
        self.assertEqual(status, 200)
        self.assertEqual(processed["processed"], 2)
        self.assertEqual(processed["delivered"], 2)
        status, forbidden, _ = self.request(
            "POST", "/outbox/process", {"max": 1}, role="write"
        )
        self.assertEqual(status, 403)
        self.assertEqual(forbidden["error"]["code"], "forbidden")

    def test_retry_requeue_dispatcher_metrics_openapi_and_server_shutdown(self):
        _, fail_webhook, _ = self.create_webhook(
            url="https://fail.invalid/hook", events=["product.created"], max_attempts=1
        )
        _, flaky_webhook, _ = self.create_webhook(
            url="https://flaky.invalid/hook",
            events=["product.created"],
            max_attempts=3,
            backoff_base_ms=0,
        )
        self.request("POST", "/products", self.product_payload())
        status, outbox_list, _ = self.request("GET", "/outbox", role="read")
        self.assertEqual(status, 200)
        self.assertEqual(len(outbox_list["items"]), 2)
        fail_entry = next(
            item
            for item in outbox_list["items"]
            if item["webhook_id"] == fail_webhook["id"]
        )
        flaky_entry = next(
            item
            for item in outbox_list["items"]
            if item["webhook_id"] == flaky_webhook["id"]
        )
        status, counts, _ = self.request(
            "POST", "/outbox/process", {"max": 100}, role="admin"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            counts,
            {"processed": 2, "delivered": 0, "retrying": 1, "failed": 1},
        )
        status, failed, _ = self.request(
            "GET", f"/outbox/{fail_entry['id']}", role="read"
        )
        self.assertEqual(status, 200)
        self.assertEqual(failed["status"], "failed")
        self.assertIn("sha256=", failed["attempts"][0]["signature"])
        status, retrying, _ = self.request(
            "GET", f"/outbox/{flaky_entry['id']}", role="read"
        )
        self.assertEqual(retrying["status"], "retrying")
        status, pending, _ = self.request(
            "POST", f"/outbox/{fail_entry['id']}/requeue", role="write"
        )
        self.assertEqual(status, 200)
        self.assertEqual(pending["status"], "pending")
        status, _, _ = self.request(
            "POST", f"/outbox/{flaky_entry['id']}/requeue", role="write"
        )
        self.assertEqual(status, 409)
        self.request("POST", "/outbox/process", {"ignore_schedule": True}, role="admin")
        self.request("POST", "/outbox/process", {"ignore_schedule": True}, role="admin")
        status, delivered, _ = self.request(
            "GET", f"/outbox/{flaky_entry['id']}", role="read"
        )
        self.assertEqual(status, 200)
        self.assertEqual(delivered["status"], "delivered")

        status, dispatcher, _ = self.request("GET", "/outbox/dispatcher", role="admin")
        self.assertEqual(dispatcher, {"enabled": True, "interval_ms": 1000})
        status, partial, _ = self.request(
            "PUT", "/outbox/dispatcher", {"interval_ms": 500}, role="admin"
        )
        self.assertEqual(status, 200)
        self.assertEqual(partial, {"enabled": True, "interval_ms": 500})
        status, dispatcher, _ = self.request(
            "PUT", "/outbox/dispatcher", {"enabled": False}, role="admin"
        )
        self.assertEqual(status, 200)
        self.assertEqual(dispatcher, {"enabled": False, "interval_ms": 500})
        status, forbidden, _ = self.request("GET", "/outbox/dispatcher", role="read")
        self.assertEqual(status, 403)
        status, metrics, _ = self.request("GET", "/metrics", role="admin")
        self.assertEqual(status, 200)
        self.assertIn('agent_qa_outbox{status="delivered"} 1', metrics)
        self.assertIn('agent_qa_outbox{status="failed"} 1', metrics)
        self.assertIn("agent_qa_outbox_dropped_total", metrics)

        from agent_qa.openapi import build_openapi

        spec = build_openapi(ROUTES, "outbox-api-test")
        expected_paths = {
            "/webhooks",
            "/webhooks/{id}",
            "/outbox",
            "/outbox/{id}",
            "/outbox/process",
            "/outbox/{id}/requeue",
            "/outbox/dispatcher",
        }
        self.assertTrue(expected_paths.issubset(spec["paths"]))
        self.assertIn("OutboxEntry", spec["components"]["schemas"])
        self.assertEqual(set(spec["paths"]["/outbox/dispatcher"]), {"get", "put"})

        class FakeServer:
            def __init__(self):
                self.closed = False

            def serve_forever(self):
                return None

            def server_close(self):
                self.closed = True

        fake_server = FakeServer()
        with (
            patch("agent_qa.server.ThreadingHTTPServer", return_value=fake_server),
            patch("agent_qa.server.config.port", return_value=8080),
            patch("agent_qa.server.JOB_RUNNER.stop") as stop_jobs,
            patch("agent_qa.server.OUTBOX.stop") as stop_outbox,
        ):
            from agent_qa.server import main

            main()
        self.assertTrue(fake_server.closed)
        stop_jobs.assert_called_once_with(timeout=5)
        stop_outbox.assert_called_once_with(timeout=5)


if __name__ == "__main__":
    unittest.main()
