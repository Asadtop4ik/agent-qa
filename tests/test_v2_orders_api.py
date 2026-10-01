"""Focused socket-free tests for the v2 order route adapters."""

import json
import unittest
import uuid
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from agent_qa.audit import AUDIT_LOG
from agent_qa.context import RequestContext, clear_context, set_context
from agent_qa.errors import ApiError
from agent_qa.idempotency import IdempotencyStore
from agent_qa.orders import OrderStore
from agent_qa.products import ProductStore
from agent_qa.routes import (
    ROUTES,
    create_order_v2,
    delete_order_v2,
    get_order,
    get_order_v2,
    list_orders_v2,
    patch_order_v2,
)
from agent_qa.server import Handler
from tests.test_content_negotiation_handler import make_handler, response_header


class V2OrdersRouteTests(unittest.TestCase):
    def setUp(self):
        self.orders = OrderStore()
        self.products = ProductStore()
        self.orders_patch = patch("agent_qa.routes.ORDER_STORE", self.orders)
        self.products_patch = patch("agent_qa.routes.PRODUCT_STORE", self.products)
        self.outbox_patch = patch("agent_qa.routes.OUTBOX.emit")
        self.orders_patch.start()
        self.products_patch.start()
        self.emit = self.outbox_patch.start()

    def _dispatch(self, method, path, payload=None, headers=None):
        request_headers = dict(headers or {})
        raw_body = b""
        if payload is not None:
            raw_body = json.dumps(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
            request_headers["Content-Length"] = str(len(raw_body))
        handler = make_handler(path, method=method, headers=request_headers)
        handler.rfile = BytesIO(raw_body)
        handler._request_started = 0
        handler._record_response = lambda status: Handler._record_response(
            handler, status
        )
        set_context(RequestContext(request_id=f"v2-test-{uuid.uuid4().hex[:12]}"))
        decision = SimpleNamespace(
            allowed=True,
            limit=100,
            remaining=99,
            reset_after=1,
        )
        try:
            with (
                patch(
                    "agent_qa.server.authenticate_api_key",
                    return_value={
                        "key_id": "v2-test-key",
                        "role": "write",
                        "label": "test",
                    },
                ),
                patch("agent_qa.server.RATE_LIMITER.consume", return_value=decision),
                patch("agent_qa.server.write_access_log"),
            ):
                Handler._handle(handler)
        finally:
            clear_context()
        response = handler.wfile.getvalue()
        body = json.loads(response) if response else None
        return handler, body

    def tearDown(self):
        self.outbox_patch.stop()
        self.products_patch.stop()
        self.orders_patch.stop()

    @staticmethod
    def _create_v2(payload):
        return create_order_v2([], payload=payload)

    def test_v2_item_create_shares_order_inventory_etag_and_events(self):
        product = self.products.create(
            sku="SHARED-01",
            name="Shared item",
            category="gear",
            price_cents=125,
            stock=5,
        )
        status, body, headers = self._create_v2(
            {
                "customer": {"id": "shared-customer"},
                "items": [{"product_id": product["id"], "quantity": 2}],
            }
        )
        self.assertEqual(status, 201)
        self.assertEqual(headers["Location"], f"/v2/orders/{body['id']}")
        self.assertEqual(
            set(body),
            {
                "id",
                "customer",
                "amount",
                "status",
                "items",
                "created_at",
                "links",
            },
        )
        self.assertEqual(body["customer"], {"id": "shared-customer"})
        self.assertEqual(body["amount"], {"total_cents": 250, "currency": "USD"})
        self.assertEqual(self.products.get(product["id"])["stock"], 3)

        status, _, v1_headers = get_order([], {"id": str(body["id"])})
        v2_status, v2_body, v2_headers = get_order_v2([], {"id": str(body["id"])})
        self.assertEqual((status, v2_status), (200, 200))
        self.assertEqual(v1_headers["ETag"], headers["ETag"])
        self.assertEqual(v2_headers["ETag"], headers["ETag"])
        self.assertNotIn("customer_id", v2_body)
        self.assertNotIn("total_cents", v2_body)
        self.emit.assert_called_once()
        self.assertEqual(self.emit.call_args.args[0], "order.created")

    def test_v2_cursor_pages_use_shared_store_and_default_to_cursor(self):
        for number in range(3):
            self._create_v2(
                {
                    "customer": {"id": "cursor-customer"},
                    "amount": {"total_cents": number + 1},
                }
            )
        status, first, _ = list_orders_v2([("limit", "2")])
        self.assertEqual(status, 200)
        self.assertEqual(set(first), {"data", "page"})
        self.assertEqual(first["page"]["limit"], 2)
        self.assertEqual(first["page"]["total"], 3)
        self.assertIsNotNone(first["page"]["next_cursor"])
        status, second, _ = list_orders_v2(
            [("limit", "2"), ("cursor", first["page"]["next_cursor"])]
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(second["data"]), 1)
        self.assertIsNone(second["page"]["next_cursor"])
        self.assertEqual(
            list_orders_v2([("customer", "cursor-customer")])[1]["page"]["total"],
            3,
        )

    def test_v2_if_match_patch_and_delete_share_v1_etag(self):
        _, created, headers = self._create_v2(
            {"customer": {"id": "etag-customer"}, "amount": {"total_cents": 40}}
        )
        order_path = {"id": str(created["id"])}
        old_etag = headers["ETag"]
        status, updated, new_headers = patch_order_v2(
            [], order_path, {"status": "paid"}, {"If-Match": old_etag}
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["status"], "paid")
        self.assertNotEqual(new_headers["ETag"], old_etag)
        with self.assertRaises(ApiError) as caught:
            delete_order_v2([], order_path, request_headers={"If-Match": old_etag})
        self.assertEqual(caught.exception.status, 412)

    def test_v2_delete_releases_stock_reserved_by_shared_fulfillment(self):
        product = self.products.create(
            sku="DELETE-01",
            name="Delete item",
            category="gear",
            price_cents=25,
            stock=4,
        )
        _, created, _ = self._create_v2(
            {
                "customer": {"id": "delete-customer"},
                "items": [{"product_id": product["id"], "quantity": 3}],
            }
        )
        etag = get_order_v2([], {"id": str(created["id"])})[2]["ETag"]
        status, _, _ = delete_order_v2(
            [],
            {"id": str(created["id"])},
            request_headers={"If-Match": etag},
        )
        self.assertEqual(status, 204)
        self.assertEqual(self.products.get(product["id"])["stock"], 4)
        self.assertEqual(self.emit.call_args_list[-1].args[0], "order.deleted")

    def test_v2_validation_uses_v2_field_paths_and_bounds_external_values(self):
        invalid_payloads = (
            (
                {"customer": {"id": "ok"}, "amount": {"total_cents": True}},
                "amount.total_cents",
            ),
            (
                {"customer": {"id": "ok"}, "amount": {"total_cents": 10**100}},
                "amount.total_cents",
            ),
            ({"customer": "wrong", "amount": {"total_cents": 1}}, "customer"),
            ({"customer": {"id": "ok"}, "amount": 1}, "amount"),
            (
                {
                    "customer": {"id": "ok"},
                    "items": [{"product_id": 1, "quantity": 1001}],
                },
                "items[0].quantity",
            ),
            (
                {
                    "customer": {"id": "ok"},
                    "items": [{"product_id": 1, "quantity": True}],
                },
                "items[0].quantity",
            ),
            (
                {
                    "customer": {"id": "ok"},
                    "items": [{"product_id": 1, "quantity": 1}],
                    "amount": {"total_cents": 1},
                },
                "items",
            ),
            ({"customer": {}, "amount": {"total_cents": 1}}, "customer.id"),
        )
        for payload, expected_field in invalid_payloads:
            with self.subTest(expected_field=expected_field, payload=payload):
                with self.assertRaises(ApiError) as caught:
                    self._create_v2(payload)
                self.assertEqual(caught.exception.status, 400)
                self.assertIn(
                    expected_field,
                    [error["field"] for error in caught.exception.details],
                )
        self.assertEqual(self.orders.list()[1], 0)

    def test_v2_query_rejects_offsets_and_unrecognized_legacy_pagination(self):
        for query in (
            [("offset", "0")],
            [("pagination", "cursor")],
            [("limit", str(10**100))],
        ):
            with self.subTest(query=query):
                with self.assertRaises(ApiError) as caught:
                    list_orders_v2(query)
                self.assertEqual(caught.exception.status, 400)

    def test_v2_cursor_rejects_changed_filter_or_sort(self):
        for _ in range(2):
            self._create_v2(
                {
                    "customer": {"id": "cursor-filter"},
                    "amount": {"total_cents": 1},
                }
            )
        first = list_orders_v2([("limit", "1"), ("customer", "cursor-filter")])[1]
        cursor = first["page"]["next_cursor"]
        with self.assertRaises(ApiError) as caught:
            list_orders_v2(
                [
                    ("limit", "1"),
                    ("customer", "cursor-filter"),
                    ("sort", "-id"),
                    ("cursor", cursor),
                ]
            )
        self.assertEqual(caught.exception.code, "cursor_mismatch")

    def test_v2_route_table_advertises_idempotent_versioned_crud(self):
        v2 = [route for route in ROUTES if str(route["path"]).startswith("/v2/orders")]
        self.assertEqual(
            {(route["method"], route["path"]) for route in v2},
            {
                ("GET", "/v2/orders"),
                ("POST", "/v2/orders"),
                ("GET", "/v2/orders/{id}"),
                ("PATCH", "/v2/orders/{id}"),
                ("DELETE", "/v2/orders/{id}"),
            },
        )
        self.assertTrue(all(route.get("api_version") == "2" for route in v2))
        create = next(route for route in v2 if route["method"] == "POST")
        self.assertTrue(create["idempotent"])

    def test_http_v1_create_v2_read_and_patch_v1_read_share_the_resource(self):
        created, v1_body = self._dispatch(
            "POST",
            "/orders",
            {"customer_id": "cross-version", "total_cents": 35},
            {"Idempotency-Key": f"cross-{uuid.uuid4().hex}"},
        )
        self.assertEqual(created.status, 201)
        order_id = v1_body["id"]

        fetched, v2_body = self._dispatch("GET", f"/v2/orders/{order_id}")
        self.assertEqual(fetched.status, 200)
        self.assertEqual(v2_body["customer"], {"id": "cross-version"})
        self.assertEqual(v2_body["amount"]["total_cents"], 35)

        updated, _ = self._dispatch(
            "PATCH",
            f"/v2/orders/{order_id}",
            {"status": "paid"},
            {"If-Match": response_header(fetched, "ETag")},
        )
        self.assertEqual(updated.status, 200)
        legacy, v1_updated = self._dispatch("GET", f"/orders/{order_id}")
        self.assertEqual(legacy.status, 200)
        self.assertEqual(v1_updated["status"], "paid")

    def test_http_v2_idempotency_replay_reserves_stock_emits_and_audits_once(self):
        product = self.products.create(
            sku="REPLAY-01",
            name="Replay item",
            category="gear",
            price_cents=20,
            stock=5,
        )
        idempotency_key = f"replay-{uuid.uuid4().hex}"
        body = {
            "customer": {"id": "replay-customer"},
            "items": [{"product_id": product["id"], "quantity": 2}],
        }
        previous_seq = AUDIT_LOG.last_seq
        with patch("agent_qa.server.IDEMPOTENCY_STORE", IdempotencyStore()):
            first, first_body = self._dispatch(
                "POST",
                "/v2/orders",
                body,
                {"Idempotency-Key": idempotency_key},
            )
            replay, replay_body = self._dispatch(
                "POST",
                "/v2/orders",
                body,
                {"Idempotency-Key": idempotency_key},
            )
        self.assertEqual(first.status, 201)
        self.assertEqual(replay.status, 201)
        self.assertEqual(first_body, replay_body)
        self.assertEqual(response_header(replay, "Idempotent-Replay"), "true")
        self.assertEqual(self.products.get(product["id"])["stock"], 3)
        self.assertEqual(self.orders.list()[1], 1)
        self.emit.assert_called_once()
        entries = AUDIT_LOG.query(method="POST", since_seq=previous_seq, order="asc")[
            "items"
        ]
        self.assertEqual(len(entries), 2)
        self.assertNotIn("replay", entries[0])
        self.assertTrue(entries[1]["replay"])

    def test_http_v2_reusing_idempotency_key_rejects_changed_payload(self):
        payload = {"customer": {"id": "key-reuse"}, "amount": {"total_cents": 20}}
        headers = {"Idempotency-Key": "same-key"}
        with patch("agent_qa.server.IDEMPOTENCY_STORE", IdempotencyStore()):
            created, _ = self._dispatch("POST", "/v2/orders", payload, headers)
            payload["amount"]["total_cents"] = 21
            rejected, body = self._dispatch("POST", "/v2/orders", payload, headers)
        self.assertEqual(created.status, 201)
        self.assertEqual(rejected.status, 422)
        self.assertEqual(body["code"], "idempotency_key_reused")
        self.assertEqual(self.orders.list()[1], 1)
        self.assertEqual(self.orders.get(1)["total_cents"], 20)
        self.emit.assert_called_once()

    def test_http_v2_errors_negotiate_problem_json_and_reject_non_json(self):
        for accept in ("application/json", None):
            headers = {"Accept": accept} if accept else {}
            with self.subTest(accept=accept):
                handler, body = self._dispatch("GET", "/v2/orders/999", headers=headers)
                self.assertEqual(handler.status, 404)
                self.assertEqual(body["code"], "order_not_found")
                self.assertEqual(
                    response_header(handler, "Content-Type"),
                    "application/problem+json; charset=utf-8",
                )

        handler, body = self._dispatch(
            "GET", "/v2/orders/999", headers={"Accept": "text/html"}
        )
        self.assertEqual(handler.status, 406)
        self.assertEqual(body["code"], "not_acceptable")
        self.assertEqual(
            response_header(handler, "Content-Type"),
            "application/problem+json; charset=utf-8",
        )

    def test_http_v1_order_errors_keep_deprecation_headers(self):
        handler, body = self._dispatch("GET", "/orders/999")
        self.assertEqual(handler.status, 404)
        self.assertEqual(body["error"]["code"], "order_not_found")
        self.assertEqual(response_header(handler, "Deprecation"), "@1790812800")
        self.assertEqual(
            response_header(handler, "Sunset"),
            "Thu, 31 Dec 2026 23:59:59 GMT",
        )
        self.assertEqual(
            response_header(handler, "Link"),
            '</v2/orders/999>; rel="successor-version"',
        )
        self.assertEqual(response_header(handler, "X-API-Version"), "1")


if __name__ == "__main__":
    unittest.main()
