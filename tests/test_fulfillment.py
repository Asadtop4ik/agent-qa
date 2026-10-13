"""Tests for atomic order fulfillment and inventory reservations."""

import io
import json
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from agent_qa import orders, products
from agent_qa.fulfillment import Fulfillment
from agent_qa.server import Handler


API_KEY = "fulfillment-test-key"


def invoke(method, path, payload=None):
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    response = {}
    handler = object.__new__(Handler)
    handler.command = method
    handler.path = path
    handler.headers = {
        "Content-Length": str(len(body)),
        "Content-Type": "application/json",
        "X-API-Key": API_KEY,
    }
    handler.rfile = io.BytesIO(body)
    handler.request_id = "fulfillment-test"
    handler._json = lambda status, body, extra=None: response.update(
        status=status, body=body, headers=extra or {}
    )
    handler._handle()
    return response


class FulfillmentApiTests(unittest.TestCase):
    def setUp(self):
        self.order_patch = patch("agent_qa.routes.ORDER_STORE", orders.OrderStore())
        self.product_patch = patch(
            "agent_qa.routes.PRODUCT_STORE", products.ProductStore()
        )
        self.auth_patch = patch(
            "agent_qa.server.is_valid_api_key",
            side_effect=lambda headers: headers.get("X-API-Key") == API_KEY,
        )
        self.order_patch.start()
        self.product_patch.start()
        self.auth_patch.start()
        self.addCleanup(self.auth_patch.stop)
        self.addCleanup(self.product_patch.stop)
        self.addCleanup(self.order_patch.stop)

    def create_product(self, *, stock=10, name="Snapshot", price=50):
        return invoke(
            "POST",
            "/products",
            {
                "sku": "SKU-" + uuid.uuid4().hex[:12].upper(),
                "name": name,
                "category": "test",
                "price_cents": price,
                "stock": stock,
            },
        )["body"]

    def test_multi_line_shortage_does_not_reserve_first_line(self):
        first = self.create_product(stock=4)
        second = self.create_product(stock=0)
        response = invoke(
            "POST",
            "/orders",
            {
                "customer_id": "atomic",
                "items": [
                    {"product_id": first["id"], "quantity": 2},
                    {"product_id": second["id"], "quantity": 1},
                ],
            },
        )
        self.assertEqual(response["status"], 409)
        self.assertEqual(
            [item["field"] for item in response["body"]["error"]["details"]],
            ["items[1].quantity"],
        )
        self.assertEqual(invoke("GET", f"/products/{first['id']}")["body"]["stock"], 4)

    def test_stock_is_reserved_then_released_only_for_cancel_or_unshipped_delete(self):
        product = self.create_product(stock=4)
        created = invoke(
            "POST",
            "/orders",
            {
                "customer_id": "cancel",
                "items": [{"product_id": product["id"], "quantity": 1}],
            },
        )["body"]
        stock_path = f"/products/{product['id']}"
        self.assertEqual(invoke("GET", stock_path)["body"]["stock"], 3)
        cancelled = invoke("PATCH", f"/orders/{created['id']}", {"status": "cancelled"})
        self.assertEqual(cancelled["status"], 200)
        self.assertEqual(invoke("GET", stock_path)["body"]["stock"], 4)
        repeated = invoke("PATCH", f"/orders/{created['id']}", {"status": "cancelled"})
        self.assertEqual(repeated["status"], 409)
        self.assertEqual(invoke("GET", stock_path)["body"]["stock"], 4)
        self.assertEqual(invoke("DELETE", f"/orders/{created['id']}")["status"], 204)
        self.assertEqual(invoke("GET", stock_path)["body"]["stock"], 4)

        deleted = invoke(
            "POST",
            "/orders",
            {
                "customer_id": "delete",
                "items": [{"product_id": product["id"], "quantity": 1}],
            },
        )["body"]
        self.assertEqual(invoke("GET", stock_path)["body"]["stock"], 3)
        self.assertEqual(invoke("DELETE", f"/orders/{deleted['id']}")["status"], 204)
        self.assertEqual(invoke("GET", stock_path)["body"]["stock"], 4)
        self.assertEqual(invoke("DELETE", f"/orders/{deleted['id']}")["status"], 404)
        self.assertEqual(invoke("GET", stock_path)["body"]["stock"], 4)

        shipped = invoke(
            "POST",
            "/orders",
            {
                "customer_id": "ship",
                "items": [{"product_id": product["id"], "quantity": 1}],
            },
        )["body"]
        invoke("PATCH", f"/orders/{shipped['id']}", {"status": "paid"})
        invoke("PATCH", f"/orders/{shipped['id']}", {"status": "shipped"})
        invoke("DELETE", f"/orders/{shipped['id']}")
        self.assertEqual(invoke("GET", stock_path)["body"]["stock"], 3)

    def test_snapshot_survives_product_update(self):
        product = self.create_product(name="Before", price=25)
        order = invoke(
            "POST",
            "/orders",
            {
                "customer_id": "snapshot",
                "items": [{"product_id": product["id"], "quantity": 2}],
            },
        )["body"]
        invoke(
            "PATCH",
            f"/products/{product['id']}",
            {"name": "After", "price_cents": 100},
        )
        fetched = invoke("GET", f"/orders/{order['id']}")["body"]
        self.assertEqual(fetched["items"][0]["name"], "Before")
        self.assertEqual(fetched["items"][0]["unit_price_cents"], 25)
        self.assertEqual(fetched["total_cents"], 50)

    def test_full_order_store_rolls_back_reservation_without_using_id(self):
        limited_store = orders.OrderStore(capacity=1)
        with patch("agent_qa.routes.ORDER_STORE", limited_store):
            first = invoke(
                "POST", "/orders", {"customer_id": "fills", "total_cents": 1}
            )
            product = self.create_product(stock=3)
            before = invoke("GET", f"/products/{product['id']}")["body"]
            failed = invoke(
                "POST",
                "/orders",
                {
                    "customer_id": "full",
                    "items": [{"product_id": product["id"], "quantity": 1}],
                },
            )
            self.assertEqual(failed["status"], 409)
            self.assertEqual(failed["body"]["error"]["code"], "store_full")
            self.assertEqual(
                invoke("GET", f"/products/{product['id']}")["body"], before
            )
            invoke("DELETE", f"/orders/{first['body']['id']}")
            next_order = invoke(
                "POST",
                "/orders",
                {
                    "customer_id": "after-full",
                    "items": [{"product_id": product["id"], "quantity": 1}],
                },
            )
            self.assertEqual(next_order["status"], 201)
            self.assertEqual(next_order["body"]["id"], first["body"]["id"] + 1)

    def test_unexpected_order_creation_failure_restores_exact_product_state(self):
        order_store = orders.OrderStore()
        product_store = products.ProductStore()
        product = product_store.create(
            sku="INJECT-FAIL",
            name="Rollback",
            category="test",
            price_cents=40,
            stock=3,
        )
        before = product_store.get(product["id"])
        fulfillment = Fulfillment(order_store, product_store)
        with patch.object(
            order_store, "_create_locked", side_effect=RuntimeError("injected")
        ):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                fulfillment.create(
                    customer_id="rollback",
                    items=[{"product_id": product["id"], "quantity": 1}],
                )
        self.assertEqual(product_store.get(product["id"]), before)
        self.assertEqual(order_store.list()[1], 0)
        self.assertEqual(order_store._next_id, 1)

    def test_thirty_requests_reserve_exactly_ten_units(self):
        product = self.create_product(stock=10)
        barrier = threading.Barrier(30)

        def create_one(index):
            barrier.wait()
            return invoke(
                "POST",
                "/orders",
                {
                    "customer_id": f"race-{index}",
                    "items": [{"product_id": product["id"], "quantity": 1}],
                },
            )["status"]

        with ThreadPoolExecutor(max_workers=30) as executor:
            statuses = list(executor.map(create_one, range(30)))
        self.assertEqual(statuses.count(201), 10)
        self.assertEqual(statuses.count(409), 20)
        self.assertEqual(
            invoke("GET", f"/products/{product['id']}")["body"]["stock"], 0
        )


if __name__ == "__main__":
    unittest.main()
