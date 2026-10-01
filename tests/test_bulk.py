"""Focused tests for bulk result handling and storage transactions."""

import threading
import unittest

from agent_qa.bulk import run_bulk
from agent_qa.errors import ApiError
from agent_qa.fulfillment import FulfillmentService
from agent_qa.orders import OrderStore
from agent_qa.products import ProductStore


def product_payload(sku="ITEM-1", *, stock=0):
    return {
        "sku": sku,
        "name": "Example item",
        "category": "example",
        "price_cents": 100,
        "stock": stock,
    }


def order_payload(customer="customer", *, product_id=None, quantity=1):
    if product_id is None:
        return {"customer_id": customer, "total_cents": 100}
    return {
        "customer_id": customer,
        "items": [{"product_id": product_id, "quantity": quantity}],
    }


class RunBulkTests(unittest.TestCase):
    def test_mixed_results_keep_input_order_and_summary(self):
        def apply(item):
            if item == "bad":
                raise ApiError(409, "conflict", "Item failed", [{"field": "x"}])
            return {"value": item}

        status, body = run_bulk(["first", "bad", "last"], apply, lambda: None, False)

        self.assertEqual(status, 207)
        self.assertEqual([result["index"] for result in body["results"]], [0, 1, 2])
        self.assertEqual(
            body["results"][0],
            {"index": 0, "status": 201, "data": {"value": "first"}},
        )
        self.assertEqual(body["results"][1]["error"]["code"], "conflict")
        self.assertEqual(body["summary"], {"total": 3, "succeeded": 2, "failed": 1})

    def test_all_failures_use_unprocessable_status(self):
        def fail(_item):
            raise ApiError(400, "invalid", "Bad item")

        with self.assertNoLogs("agent_qa.bulk", level="ERROR"):
            status, body = run_bulk([{}, {}], fail, lambda: None, False)

        self.assertEqual(status, 422)
        self.assertEqual(body["summary"], {"total": 2, "succeeded": 0, "failed": 2})
        self.assertEqual([result["status"] for result in body["results"]], [400, 400])

    def test_atomic_failure_rolls_back_successes(self):
        rolled_back = []

        def apply(item):
            if item == "bad":
                raise ApiError(404, "missing", "Not found")
            return item

        status, body = run_bulk(
            ["created", "bad"], apply, lambda: rolled_back.append(True), True
        )

        self.assertEqual(rolled_back, [True])
        self.assertEqual(status, 422)
        self.assertEqual(
            body["results"][0],
            {
                "index": 0,
                "status": 424,
                "error": {
                    "code": "rolled_back",
                    "message": "Rolled back because another item failed",
                },
            },
        )
        self.assertEqual(body["results"][1]["status"], 404)
        self.assertEqual(body["summary"], {"total": 2, "succeeded": 0, "failed": 2})


class BulkStoreTests(unittest.TestCase):
    def test_bulk_items_reject_internal_create_options(self):
        products = ProductStore()
        orders = OrderStore()
        service = FulfillmentService(orders, products)
        for create_bulk, payload in (
            (products.create_bulk, product_payload()),
            (service.create_bulk, order_payload()),
        ):
            with self.subTest(create_bulk=create_bulk):
                status, body = create_bulk([{**payload, "include_version": True}])
                self.assertEqual(status, 422)
                self.assertEqual(body["results"][0]["status"], 400)
                self.assertEqual(
                    body["results"][0]["error"]["code"], "validation_error"
                )
        self.assertEqual(products._products, {})
        self.assertEqual(orders._orders, {})

    def test_product_bulk_detects_duplicate_sku_and_keeps_non_atomic_successes(self):
        store = ProductStore()
        status, body = store.create_bulk(
            [product_payload(), product_payload(), product_payload("ITEM-3")]
        )

        self.assertEqual(status, 207)
        self.assertEqual([item["status"] for item in body["results"]], [201, 409, 201])
        self.assertEqual(
            [item["data"]["id"] for item in (body["results"][0], body["results"][2])],
            [1, 2],
        )
        self.assertEqual(store._next_id, 3)

    def test_atomic_product_rollback_restores_records_and_ids(self):
        store = ProductStore()
        status, body = store.create_bulk(
            [product_payload(), product_payload(), product_payload("ITEM-3")],
            atomic=True,
        )

        self.assertEqual(status, 422)
        self.assertEqual(store._products, {})
        self.assertEqual(store._next_id, 1)
        self.assertEqual(body["results"][0]["status"], 424)
        self.assertEqual(store.create(**product_payload("AFTER"))["id"], 1)

    def test_order_bulk_reserves_stock_in_sequence_and_rolls_it_back_atomically(self):
        products = ProductStore()
        product = products.create(**product_payload(stock=1))
        before = products.get(product["id"], include_version=True)
        orders = OrderStore()
        service = FulfillmentService(orders, products)

        status, body = service.create_bulk(
            [
                order_payload("first", product_id=product["id"]),
                order_payload("second", product_id=product["id"]),
            ],
            atomic=True,
        )

        self.assertEqual(status, 422)
        self.assertEqual(body["results"][0]["status"], 424)
        self.assertEqual(body["results"][1]["status"], 409)
        self.assertEqual(orders._orders, {})
        self.assertEqual(orders._next_id, 1)
        self.assertEqual(products.get(product["id"])["stock"], 1)
        self.assertEqual(products.get(product["id"], include_version=True), before)
        self.assertEqual(orders.create("after", 100)["id"], 1)

    def test_non_atomic_order_bulk_observes_prior_stock_reservations(self):
        products = ProductStore()
        product = products.create(**product_payload(stock=1))
        orders = OrderStore()
        service = FulfillmentService(orders, products)

        status, body = service.create_bulk(
            [
                order_payload("first", product_id=product["id"]),
                order_payload("second", product_id=product["id"]),
            ],
        )

        self.assertEqual(status, 207)
        self.assertEqual([result["status"] for result in body["results"]], [201, 409])
        self.assertEqual(products.get(product["id"])["stock"], 0)
        self.assertEqual(orders._next_id, 2)

    def test_atomic_rollback_keeps_concurrent_order_ids_from_being_lost(self):
        products = ProductStore()
        orders = OrderStore(capacity=1)
        service = FulfillmentService(orders, products)
        original_create = service.create
        entered = threading.Event()
        release = threading.Event()
        concurrent_started = threading.Event()
        concurrent_finished = threading.Event()
        bulk_result = []
        concurrent_result = []

        def blocked_create(**fields):
            if fields.get("customer_id") == "fails":
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("test release timed out")
            return original_create(**fields)

        def run_batch():
            bulk_result.append(
                service.create_bulk(
                    [
                        order_payload("temporary"),
                        order_payload("fails"),
                    ],
                    atomic=True,
                )
            )

        def create_concurrently():
            concurrent_started.set()
            concurrent_result.append(orders.create("concurrent", 200))
            concurrent_finished.set()

        service.create = blocked_create
        batch_thread = threading.Thread(target=run_batch)
        batch_thread.start()
        self.assertTrue(entered.wait(1))
        concurrent_thread = threading.Thread(target=create_concurrently)
        concurrent_thread.start()
        self.assertTrue(concurrent_started.wait(1))
        self.assertFalse(concurrent_finished.wait(0.02))
        release.set()
        batch_thread.join(2)
        concurrent_thread.join(2)

        self.assertFalse(batch_thread.is_alive())
        self.assertFalse(concurrent_thread.is_alive())
        self.assertEqual(bulk_result[0][0], 422)
        self.assertEqual(concurrent_result[0]["id"], 1)
        self.assertEqual(orders._next_id, 2)


if __name__ == "__main__":
    unittest.main()
