"""Unit tests for atomic order and inventory operations."""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from agent_qa.errors import ApiError
from agent_qa.fulfillment import FulfillmentService
from agent_qa.orders import OrderError, OrderStore
from agent_qa.products import ProductStore


def product_fields(sku="ITEM-1", **changes):
    fields = {
        "sku": sku,
        "name": "Example item",
        "category": "example",
        "price_cents": 125,
        "stock": 0,
        "tags": [],
        "active": True,
    }
    fields.update(changes)
    return fields


class FulfillmentTests(unittest.TestCase):
    def setUp(self):
        self.orders = OrderStore()
        self.products = ProductStore()
        self.service = FulfillmentService(self.orders, self.products)

    def create(self, product_id, quantity=1, customer_id="customer"):
        return self.service.create(
            customer_id=customer_id,
            items=[{"product_id": product_id, "quantity": quantity}],
        )

    def test_reservation_snapshots_product_fields_and_computes_total(self):
        product = self.products.create(**product_fields(stock=4))
        order = self.create(product["id"], 2)
        self.products.update(product["id"], {"name": "Renamed", "price_cents": 999})

        self.assertEqual(order["total_cents"], 250)
        self.assertEqual(
            order["items"],
            [
                {
                    "product_id": product["id"],
                    "sku": "ITEM-1",
                    "name": "Example item",
                    "quantity": 2,
                    "unit_price_cents": 125,
                    "line_total_cents": 250,
                }
            ],
        )
        self.assertEqual(self.products.get(product["id"])["stock"], 2)

    def test_missing_duplicate_and_inactive_products_have_stable_errors(self):
        active = self.products.create(**product_fields(stock=5))
        inactive = self.products.create(
            **product_fields(sku="ITEM-2", active=False, stock=5)
        )
        cases = (
            ([{"product_id": 99, "quantity": 1}], 400, "Unknown product"),
            (
                [
                    {"product_id": active["id"], "quantity": 1},
                    {"product_id": active["id"], "quantity": 1},
                ],
                400,
                "Duplicate product",
            ),
            (
                [{"product_id": inactive["id"], "quantity": 1}],
                409,
                "Product is unavailable",
            ),
        )
        for items, status, message in cases:
            with self.subTest(message=message), self.assertRaises(ApiError) as error:
                self.service.create(customer_id="customer", items=items)
            self.assertEqual(error.exception.status, status)
            self.assertEqual(error.exception.details[0]["message"], message)

    def test_multi_line_shortage_reports_all_lines_without_mutation(self):
        first = self.products.create(**product_fields(stock=5))
        second = self.products.create(**product_fields(sku="ITEM-2", stock=1))
        before = [self.products.get(first["id"]), self.products.get(second["id"])]
        with self.assertRaises(ApiError) as error:
            self.service.create(
                customer_id="customer",
                items=[
                    {"product_id": first["id"], "quantity": 1},
                    {"product_id": second["id"], "quantity": 3},
                ],
            )
        self.assertEqual(error.exception.code, "insufficient_stock")
        self.assertEqual(
            error.exception.details,
            [{"field": "items[1].quantity", "message": "Only 1 in stock"}],
        )
        self.assertEqual(
            [self.products.get(first["id"]), self.products.get(second["id"])], before
        )
        self.assertEqual(self.orders.list()[1], 0)
        self.assertEqual(self.orders._next_id, 1)

    def test_capacity_failure_restores_stock_timestamps_and_id(self):
        orders = OrderStore(capacity=1)
        orders.create("legacy", 100)
        products = ProductStore()
        product = products.create(**product_fields(stock=3))
        before = products.get(product["id"])
        service = FulfillmentService(orders, products)

        with self.assertRaises(OrderError) as error:
            service.create(
                customer_id="customer",
                items=[{"product_id": product["id"], "quantity": 1}],
            )
        self.assertEqual(error.exception.code, "store_full")
        self.assertEqual(products.get(product["id"]), before)
        self.assertEqual(orders._next_id, 2)

    def test_computed_total_overflow_has_no_side_effects(self):
        product = self.products.create(
            **product_fields(price_cents=100_000_000, stock=3)
        )
        before = self.products.get(product["id"])

        with self.assertRaises(ApiError) as error:
            self.create(product["id"], 2)

        self.assertEqual(error.exception.status, 400)
        self.assertEqual(
            error.exception.details,
            [{"field": "items", "message": "Order total exceeds maximum"}],
        )
        self.assertEqual(self.products.get(product["id"]), before)
        self.assertEqual(self.orders._next_id, 1)

    def test_second_reservation_write_failure_restores_all_state(self):
        first = self.products.create(**product_fields(stock=3))
        second = self.products.create(**product_fields(sku="ITEM-2", stock=4))
        before = [self.products.get(first["id"]), self.products.get(second["id"])]
        change_stock = self.products._change_stock_locked
        calls = 0

        def fail_after_second_write(product_id, delta):
            nonlocal calls
            calls += 1
            result = change_stock(product_id, delta)
            if calls == 2:
                raise RuntimeError("injected stock write failure")
            return result

        self.products._change_stock_locked = fail_after_second_write
        with self.assertRaises(RuntimeError):
            self.service.create(
                customer_id="customer",
                items=[
                    {"product_id": first["id"], "quantity": 1},
                    {"product_id": second["id"], "quantity": 2},
                ],
            )
        self.products._change_stock_locked = change_stock

        self.assertEqual(
            [self.products.get(first["id"]), self.products.get(second["id"])], before
        )
        self.assertEqual(self.orders._next_id, 1)
        self.assertEqual(self.orders.list()[1], 0)

    def test_unexpected_create_failure_rolls_back_order_id_and_inventory(self):
        product = self.products.create(**product_fields(stock=3))
        before = self.products.get(product["id"])
        create_locked = self.orders._create_locked

        def fail_after_insert(fields):
            create_locked(fields)
            raise RuntimeError("injected order write failure")

        self.orders._create_locked = fail_after_insert
        with self.assertRaises(RuntimeError):
            self.create(product["id"])
        self.orders._create_locked = create_locked
        self.assertEqual(self.products.get(product["id"]), before)
        self.assertEqual(self.orders.list()[1], 0)
        self.assertEqual(self.orders._next_id, 1)

    def test_cancel_and_delete_release_once_but_shipped_does_not(self):
        product = self.products.create(**product_fields(stock=10))
        cancelled = self.create(product["id"], 2)
        self.service.update(cancelled["id"], {"status": "cancelled"})
        self.assertEqual(self.products.get(product["id"])["stock"], 10)
        with self.assertRaises(OrderError) as error:
            self.service.update(cancelled["id"], {"status": "cancelled"})
        self.assertEqual(error.exception.code, "invalid_transition")
        self.service.delete(cancelled["id"])
        self.assertEqual(self.products.get(product["id"])["stock"], 10)
        self.assertFalse(self.service.delete(cancelled["id"]))

        shipped = self.create(product["id"], 3, "second")
        self.service.update(shipped["id"], {"status": "paid"})
        self.service.update(shipped["id"], {"status": "shipped"})
        self.assertEqual(self.products.get(product["id"])["stock"], 7)
        self.service.delete(shipped["id"])
        self.assertEqual(self.products.get(product["id"])["stock"], 7)

    def test_delete_active_order_releases_and_item_total_patch_is_rejected(self):
        product = self.products.create(**product_fields(stock=4))
        order = self.create(product["id"], 2)
        with self.assertRaises(ApiError) as error:
            self.service.update(order["id"], {"total_cents": 3})
        self.assertEqual(error.exception.code, "total_computed")
        self.assertTrue(self.service.delete(order["id"]))
        self.assertEqual(self.products.get(product["id"])["stock"], 4)

    def test_failed_release_keeps_order_and_all_stock_unchanged(self):
        first = self.products.create(**product_fields(stock=5))
        second = self.products.create(**product_fields(sku="ITEM-2", stock=5))
        order = self.service.create(
            customer_id="customer",
            items=[
                {"product_id": first["id"], "quantity": 1},
                {"product_id": second["id"], "quantity": 1},
            ],
        )
        self.products.adjust_stock(first["id"], 1_000_000 - 4)
        before = [self.products.get(first["id"]), self.products.get(second["id"])]

        with self.assertRaises(ApiError):
            self.service.update(order["id"], {"status": "cancelled"})
        self.assertEqual(self.orders.get(order["id"])["status"], "new")
        self.assertEqual(
            [self.products.get(first["id"]), self.products.get(second["id"])], before
        )

    def test_total_computed_error_precedes_cancel_release_failure(self):
        product = self.products.create(**product_fields(stock=2))
        order = self.create(product["id"])
        self.products.adjust_stock(product["id"], 1_000_000 - 1)
        before = self.products.get(product["id"])

        with self.assertRaises(ApiError) as error:
            self.service.update(order["id"], {"status": "cancelled", "total_cents": 1})

        self.assertEqual(error.exception.code, "total_computed")
        self.assertEqual(self.orders.get(order["id"])["status"], "new")
        self.assertEqual(self.products.get(product["id"]), before)

    def test_second_release_write_failure_restores_all_state(self):
        first = self.products.create(**product_fields(stock=3))
        second = self.products.create(**product_fields(sku="ITEM-2", stock=3))
        order = self.service.create(
            customer_id="customer",
            items=[
                {"product_id": first["id"], "quantity": 1},
                {"product_id": second["id"], "quantity": 1},
            ],
        )
        before = [self.products.get(first["id"]), self.products.get(second["id"])]
        change_stock = self.products._change_stock_locked
        calls = 0

        def fail_after_second_write(product_id, delta):
            nonlocal calls
            calls += 1
            result = change_stock(product_id, delta)
            if calls == 2:
                raise RuntimeError("injected stock write failure")
            return result

        self.products._change_stock_locked = fail_after_second_write
        with self.assertRaises(RuntimeError):
            self.service.update(order["id"], {"status": "cancelled"})
        self.products._change_stock_locked = change_stock

        self.assertEqual(self.orders.get(order["id"])["status"], "new")
        self.assertEqual(
            [self.products.get(first["id"]), self.products.get(second["id"])], before
        )

    def test_concurrent_reservations_never_oversell(self):
        product = self.products.create(**product_fields(stock=10))
        barrier = threading.Barrier(30)

        def buy(index):
            barrier.wait(timeout=10)
            try:
                service = FulfillmentService(self.orders, self.products)
                return service.create(
                    customer_id=f"customer-{index}",
                    items=[{"product_id": product["id"], "quantity": 1}],
                )
            except ApiError as error:
                self.assertEqual(error.code, "insufficient_stock")
                return None

        with ThreadPoolExecutor(max_workers=30) as executor:
            results = list(executor.map(buy, range(30)))
        self.assertEqual(sum(result is not None for result in results), 10)
        self.assertEqual(self.products.get(product["id"])["stock"], 0)
        self.assertEqual(self.orders.list()[1], 10)

    def test_order_item_snapshots_are_defensively_copied(self):
        product = self.products.create(**product_fields(stock=5))
        created = self.create(product["id"])
        created["items"][0]["name"] = "outside mutation"
        created["items"].append({"name": "extra"})

        fetched = self.orders.get(created["id"])
        self.assertEqual(fetched["items"][0]["name"], "Example item")
        fetched["items"][0]["name"] = "get mutation"
        listed, _ = self.orders.list()
        self.assertEqual(listed[0]["items"][0]["name"], "Example item")
        listed[0]["items"][0]["name"] = "list mutation"
        updated = self.service.update(created["id"], {"status": "paid"})
        self.assertEqual(updated["items"][0]["name"], "Example item")
        updated["items"][0]["name"] = "update mutation"
        self.assertEqual(
            self.orders.get(created["id"])["items"][0]["name"], "Example item"
        )


if __name__ == "__main__":
    unittest.main()
