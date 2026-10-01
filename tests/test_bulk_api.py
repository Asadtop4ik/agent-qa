"""Socket-free dispatcher coverage for bulk creation routes."""

import json
import unittest
from email.message import Message
from io import BytesIO
from unittest.mock import patch

from agent_qa.errors import ApiError
from agent_qa.orders import OrderStore
from agent_qa.products import ProductStore
from agent_qa.routes import ROUTES
from agent_qa.server import Handler, allowed_methods


AUTH_IDENTITY = {"key_id": "test", "role": "admin", "label": "test", "tenants": None}


class BulkApiDispatchTests(unittest.TestCase):
    def setUp(self):
        self.orders = patch("agent_qa.routes.ORDER_STORE", OrderStore())
        self.products = patch("agent_qa.routes.PRODUCT_STORE", ProductStore())
        self.orders.start()
        self.products.start()
        self.auth = patch(
            "agent_qa.server.authenticate_api_key", return_value=AUTH_IDENTITY
        )
        self.auth.start()

    def tearDown(self):
        self.auth.stop()
        self.products.stop()
        self.orders.stop()

    @staticmethod
    def dispatch(method, path, payload=None, raw_body=None):
        raw = raw_body if raw_body is not None else json.dumps(payload).encode("utf-8")
        headers = Message()
        headers["Content-Length"] = str(len(raw))
        headers["Content-Type"] = "application/json"
        responses = []
        handler = object.__new__(Handler)
        handler.path = path
        handler.command = method
        handler.headers = headers
        handler.rfile = BytesIO(raw)
        handler.request_id = "bulk-api-test"
        handler._json = lambda *args: responses.append(args)
        try:
            Handler._dispatch(handler)
        except ApiError as error:
            return (
                error.status,
                {"error": {"code": error.code, "details": error.details}},
                {},
            )
        status, body, response_headers = responses[0]
        return status, body, response_headers

    @staticmethod
    def legacy_order(customer):
        return {"customer_id": customer, "total_cents": 100}

    def test_all_success_and_atomic_failure_statuses(self):
        product = {
            "sku": "BULK-1",
            "name": "Bulk product",
            "category": "example",
            "price_cents": 100,
        }
        status, body, headers = self.dispatch(
            "POST", "/products/bulk", {"items": [product]}
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["summary"], {"total": 1, "succeeded": 1, "failed": 0})
        self.assertNotIn("version", body["results"][0]["data"])
        self.assertNotIn("Location", headers)

        status, body, _ = self.dispatch(
            "POST",
            "/orders/bulk",
            {"items": [self.legacy_order("temporary"), {}], "atomic": True},
        )
        self.assertEqual(status, 422)
        self.assertEqual([item["status"] for item in body["results"]], [424, 400])
        status, body, _ = self.dispatch(
            "POST", "/orders/bulk", {"items": [self.legacy_order("after")]}
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["results"][0]["data"]["id"], 1)
        status, order, _ = self.dispatch("GET", "/orders/1")
        self.assertEqual(status, 200)
        self.assertEqual(order, body["results"][0]["data"])

    def test_order_bulk_returns_item_order_and_aggregate_status(self):
        status, body, headers = self.dispatch(
            "POST",
            "/orders/bulk",
            {
                "items": [
                    self.legacy_order("first"),
                    {},
                    7,
                    self.legacy_order("last"),
                ]
            },
        )
        self.assertEqual(status, 207)
        self.assertEqual([item["index"] for item in body["results"]], [0, 1, 2, 3])
        self.assertEqual(
            [item["status"] for item in body["results"]], [201, 400, 400, 201]
        )
        self.assertEqual(body["summary"], {"total": 4, "succeeded": 2, "failed": 2})
        self.assertNotIn("Location", headers)

    def test_bulk_validates_envelope_and_uses_the_64kib_limit(self):
        too_many = self.dispatch(
            "POST", "/orders/bulk", {"items": [self.legacy_order("x")] * 51}
        )
        self.assertEqual(too_many[0], 400)
        self.assertEqual(too_many[1]["error"]["code"], "validation_error")

        extra_property = self.dispatch(
            "POST", "/orders/bulk", {"items": [self.legacy_order("x")], "other": 1}
        )
        self.assertEqual(extra_property[0], 400)
        self.assertEqual(extra_property[1]["error"]["code"], "validation_error")

        too_large = self.dispatch("POST", "/products/bulk", raw_body=b" " * 70000)
        self.assertEqual(too_large[0], 413)
        self.assertEqual(too_large[1]["error"]["code"], "payload_too_large")

    def test_bulk_literal_paths_have_only_post_in_allow(self):
        for path in ("/orders/bulk", "/products/bulk"):
            self.assertEqual(allowed_methods(path), "POST")
        route_paths = {route["path"] for route in ROUTES}
        self.assertIn("/orders/bulk", route_paths)
        self.assertIn("/products/bulk", route_paths)

    def test_bulk_accepts_limit_and_rejects_malformed_input(self):
        payload = {"items": [self.legacy_order("x")] * 50}
        raw = json.dumps(payload).encode().ljust(65536, b" ")
        status, body, _ = self.dispatch("POST", "/orders/bulk", raw_body=raw)
        self.assertEqual(status, 201)
        self.assertEqual(len(body["results"]), 50)
        for invalid in (
            {"items": []},
            {"items": {}},
            {"items": [self.legacy_order("x")], "atomic": 1},
        ):
            with self.subTest(payload=invalid):
                status, body, _ = self.dispatch("POST", "/orders/bulk", invalid)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "validation_error")
        for raw in (
            b'{"items":[' + b"[" * 40 + b"0" + b"]" * 40 + b"]}",
            b'{"items":[{"total_cents":' + b"9" * 5000 + b"}]}",
        ):
            with self.subTest(raw_size=len(raw)):
                status, body, _ = self.dispatch("POST", "/orders/bulk", raw_body=raw)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "invalid_json")

    def test_metrics_keep_bulk_route_as_a_literal_label(self):
        from agent_qa.server import REGISTRY

        handler = object.__new__(Handler)
        handler.path = "/orders/bulk"
        handler.command = "POST"
        handler._request_started = 0
        handler._response_recorded = False
        handler.request_id = "bulk-metrics-test"
        with (
            patch.object(REGISTRY, "record") as record,
            patch("agent_qa.server.write_access_log"),
        ):
            Handler._record_response(handler, 201)
        self.assertEqual(record.call_args.args[1:3], ("/orders/bulk", 201))
