import unittest
from concurrent.futures import ThreadPoolExecutor
import threading

from agent_qa.conditional import PreconditionFailed, etag_for
from agent_qa.orders import (
    MAX_ORDERS,
    OrderError,
    OrderStore,
    validate_create,
    validate_patch,
    validate_query,
)


class OrderValidationTests(unittest.TestCase):
    def test_create_reports_all_errors_in_field_order(self):
        with self.assertRaises(OrderError) as error:
            validate_create({"status": "paid", "total_cents": True})
        self.assertEqual(error.exception.code, "validation_error")
        self.assertEqual(
            [detail["field"] for detail in error.exception.details],
            ["customer_id", "status", "total_cents"],
        )

    def test_create_validates_customer_and_total(self):
        invalid = (
            {"customer_id": " ", "total_cents": 0},
            {"customer_id": "x" * 65, "total_cents": 0},
            {"customer_id": "x", "total_cents": -1},
            {"customer_id": "x", "total_cents": 100_000_001},
            {"customer_id": "x", "total_cents": True},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(OrderError):
                validate_create(payload)

    def test_patch_requires_fields_and_rejects_unknown_fields(self):
        for payload in ({}, {"id": 1}, {"status": []}):
            with self.subTest(payload=payload), self.assertRaises(OrderError):
                validate_patch(payload)

    def test_query_validates_values_and_repeated_parameters(self):
        self.assertEqual(
            validate_query([("limit", "2"), ("offset", "1")]),
            {"limit": 2, "sort": "id", "pagination": "offset", "offset": 1},
        )
        self.assertEqual(
            validate_query([("cursor", "opaque")]),
            {"limit": 20, "sort": "id", "pagination": "cursor", "cursor": "opaque"},
        )
        invalid_queries = (
            [("limit", None)],
            [(None, "1")],
            ["malformed"],
            [("limit", "0")],
            [("limit", "101")],
            [("limit", "abc")],
            [("offset", "-1")],
            [("status", "foo")],
            [("x", "1")],
            [("limit", "1"), ("limit", "2")],
            [("cursor", "abc"), ("offset", "0")],
            [("pagination", "offset"), ("cursor", "abc")],
        )
        for query in invalid_queries:
            with self.subTest(query=query), self.assertRaises(OrderError) as error:
                validate_query(query)
            self.assertEqual(error.exception.code, "invalid_query")


class OrderStoreTests(unittest.TestCase):
    def test_versions_are_hidden_by_default_and_preconditions_are_atomic(self):
        store = OrderStore()
        created = store.create("customer-a", 1500, include_version=True)
        self.assertEqual(created["version"], 1)
        self.assertNotIn("version", store.get(created["id"]))

        expected = (etag_for("order", created["id"], 1),)
        barrier = threading.Barrier(20)

        def patch(_):
            barrier.wait(timeout=10)
            try:
                return store.update(1, {"status": "paid"}, expected_version=expected)
            except PreconditionFailed:
                return None

        with ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(patch, range(20)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(store.get(1, include_version=True)["version"], 2)

    def test_created_order_fields_and_monotonic_ids_after_delete(self):
        store = OrderStore()
        first = store.create("customer-a", 1500)
        self.assertEqual(first["id"], 1)
        self.assertEqual(first["status"], "new")
        self.assertTrue(first["created_at"].endswith("Z"))
        self.assertIsInstance(first["total_cents"], int)
        self.assertEqual(first["items"], [])

        self.assertTrue(store.delete(first["id"]))
        second = store.create("customer-b", 2000)
        self.assertEqual(second["id"], 2)
        self.assertFalse(store.delete(999))

    def test_get_returns_independent_copy_and_invalid_ids_are_missing(self):
        store = OrderStore()
        order = store.create("customer-a", 100)
        copy = store.get(order["id"])
        copy["status"] = "paid"
        copy["items"].append({"name": "external"})
        self.assertEqual(store.get(order["id"])["status"], "new")
        self.assertEqual(store.get(order["id"])["items"], [])
        for invalid_id in (0, -1, "1", True):
            self.assertIsNone(store.get(invalid_id))
            self.assertFalse(store.delete(invalid_id))

    def test_list_filters_sorts_and_paginates(self):
        store = OrderStore()
        one = store.create("same", 1)
        store.create("other", 2)
        three = store.create("same", 3)
        store.update(one["id"], {"status": "paid"})

        items, total = store.list(customer_id="same", limit=1, offset=1)
        self.assertEqual(total, 2)
        self.assertEqual([item["id"] for item in items], [three["id"]])
        items, total = store.list(status="paid")
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["id"], one["id"])
        descending, total = store.list(sort="-id", limit=1, offset=1)
        self.assertEqual(total, 3)
        self.assertEqual([item["id"] for item in descending], [2])

        cursor_items, _, cursor = store.list(sort="-id", limit=1, pagination="cursor")
        seen = [item["id"] for item in cursor_items]
        while cursor is not None:
            cursor_items, _, cursor = store.list(sort="-id", limit=1, cursor=cursor)
            seen.extend(item["id"] for item in cursor_items)
        self.assertEqual(seen, [3, 2, 1])

    def test_cursor_pages_survive_creates_and_deletes_without_repeating(self):
        store = OrderStore()
        for index in range(50):
            store.create(f"customer-{index}", index)

        items, total, cursor = store.list(limit=7, pagination="cursor")
        seen = [item["id"] for item in items]
        self.assertEqual(total, 50)
        self.assertIsNotNone(cursor)
        store.create("new-after-page-one", 100)
        self.assertTrue(store.delete(3))
        self.assertTrue(store.delete(10))

        while cursor is not None:
            items, total, cursor = store.list(
                limit=5 if len(seen) % 2 else 7,
                pagination="cursor",
                cursor=cursor,
            )
            seen.extend(item["id"] for item in items)
        expected = [*range(1, 10), *range(11, 52)]
        self.assertEqual(seen, expected)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(total, 49)

    def test_list_bounds_direct_pagination_values(self):
        store = OrderStore()
        for query in (
            {"limit": 10**100, "pagination": "cursor"},
            {"limit": True, "pagination": "cursor"},
            {"offset": 10**100},
        ):
            with self.subTest(query=query), self.assertRaises(OrderError) as error:
                store.list(**query)
            self.assertEqual(error.exception.code, "invalid_query")

    def test_transition_rules_and_total_lock(self):
        store = OrderStore()
        order = store.create("customer-a", 100)
        changed = store.update(order["id"], {"total_cents": 200})
        self.assertEqual(changed["total_cents"], 200)
        paid = store.update(order["id"], {"status": "paid"})
        self.assertEqual(paid["status"], "paid")

        cases = (
            ({"status": "paid"}, "invalid_transition"),
            ({"status": "new"}, "invalid_transition"),
            ({"total_cents": 300}, "order_locked"),
        )
        for changes, code in cases:
            with self.subTest(changes=changes), self.assertRaises(OrderError) as error:
                store.update(order["id"], changes)
            self.assertEqual(error.exception.code, code)

        shipped = store.update(order["id"], {"status": "shipped"})
        self.assertEqual(shipped["status"], "shipped")
        with self.assertRaises(OrderError) as error:
            store.update(order["id"], {"status": "cancelled"})
        self.assertEqual(error.exception.code, "invalid_transition")

    def test_capacity_is_limited_to_1000_active_orders(self):
        store = OrderStore()
        for index in range(MAX_ORDERS):
            store.create(f"customer-{index}", index)
        with self.assertRaises(OrderError) as error:
            store.create("overflow", 0)
        self.assertEqual(error.exception.code, "store_full")
        self.assertTrue(store.delete(1))
        self.assertEqual(store.create("after-delete", 1)["id"], MAX_ORDERS + 1)

    def test_concurrent_creates_allocate_unique_ids(self):
        store = OrderStore(capacity=100)
        with ThreadPoolExecutor(max_workers=8) as executor:
            orders = list(
                executor.map(
                    lambda index: store.create(f"customer-{index}", index), range(100)
                )
            )
        self.assertEqual(sorted(order["id"] for order in orders), list(range(1, 101)))
        self.assertEqual(store.list(limit=100)[1], 100)


if __name__ == "__main__":
    unittest.main()
