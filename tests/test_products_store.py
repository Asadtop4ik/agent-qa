"""Tests for product validation, storage, and atomic stock updates."""

import unittest
from concurrent.futures import ThreadPoolExecutor
import threading

from agent_qa.conditional import PreconditionFailed
from agent_qa.products import (
    MAX_PRODUCTS,
    ProductStore,
    validate_create,
    validate_patch,
    validate_query,
)
from agent_qa.errors import ApiError
from agent_qa.schemas import SCHEMAS
from agent_qa.validation import validate


def product_fields(**changes):
    fields = {
        "sku": "ITEM-1",
        "name": "Example item",
        "category": "example",
        "price_cents": 100,
        "stock": 0,
        "tags": [],
        "active": True,
    }
    fields.update(changes)
    return fields


class ProductValidationTests(unittest.TestCase):
    def test_create_applies_defaults_and_rejects_invalid_fields(self):
        self.assertEqual(
            validate_create(
                {
                    "sku": "ITEM-1",
                    "name": "Thing",
                    "category": "items",
                    "price_cents": 0,
                }
            ),
            {
                "sku": "ITEM-1",
                "name": "Thing",
                "category": "items",
                "price_cents": 0,
                "stock": 0,
                "tags": [],
                "active": True,
            },
        )
        invalid = (
            {**product_fields(), "sku": "lower"},
            {**product_fields(), "sku": "ITEM-1\n"},
            {**product_fields(), "name": " "},
            {**product_fields(), "category": "Bad"},
            {**product_fields(), "price_cents": True},
            {**product_fields(), "stock": 1_000_001},
            {**product_fields(), "tags": ["repeat", "repeat"]},
            {**product_fields(), "unknown": "field"},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ApiError):
                validate_create(payload)

    def test_patch_rejects_immutable_sku_and_requires_a_change(self):
        for payload in ({}, {"sku": None}, {"stock": True}, {"extra": 1}):
            with self.subTest(payload=payload), self.assertRaises(ApiError):
                validate_patch(payload)

        with self.assertRaises(ApiError) as error:
            validate_patch({"sku": "NEW-SKU"})
        self.assertEqual(error.exception.details[0]["field"], "sku")
        self.assertEqual(error.exception.details[0]["message"], "Cannot be changed")
        self.assertEqual(validate(SCHEMAS["UpdateProduct"], {"sku": None}), [])

    def test_query_validates_filters_sort_and_bounds(self):
        self.assertEqual(
            validate_query([("active", "false"), ("limit", "2")]),
            {"active": False, "limit": 2, "offset": 0, "sort": "id"},
        )
        invalid_queries = (
            [("active", "yes")],
            [("in_stock", "no")],
            [("sort", "-created_at")],
            [("q", "")],
            [("q", "x" * 65)],
            [("min_price_cents", "2"), ("max_price_cents", "1")],
            [("limit", "999999999999999999999999999999999999")],
            [("unknown", "x")],
            [("limit", "1"), ("limit", "2")],
        )
        for query in invalid_queries:
            with self.subTest(query=query), self.assertRaises(ApiError) as error:
                validate_query(query)
            self.assertEqual(error.exception.code, "invalid_query")

    def test_product_schema_is_shared_and_bounds_fields(self):
        self.assertEqual(validate(SCHEMAS["CreateProduct"], product_fields()), [])
        self.assertTrue(
            validate(
                SCHEMAS["CreateProduct"],
                {**product_fields(), "stock": 1_000_001},
            )
        )
        self.assertTrue(
            validate(
                SCHEMAS["CreateProduct"],
                {**product_fields(), "tags": ["sale", "sale"]},
            )
        )


class ProductStoreTests(unittest.TestCase):
    def test_versions_increment_for_updates_and_stock_changes(self):
        store = ProductStore()
        created = store.create(**product_fields(stock=3), include_version=True)
        self.assertEqual(created["version"], 1)
        self.assertNotIn("version", store.get(created["id"]))
        updated = store.update(created["id"], {"name": "Changed"}, include_version=True)
        self.assertEqual(updated["version"], 2)
        adjusted = store.adjust_stock(
            created["id"], -1, expected_version=('"p1.2"',), include_version=True
        )
        self.assertEqual(adjusted["version"], 3)
        before = store.get(created["id"])
        with self.assertRaises(PreconditionFailed) as error:
            store.adjust_stock(created["id"], -1, expected_version=('"p1.2"',))
        self.assertEqual(error.exception.current_etag, '"p1.3"')
        self.assertEqual(store.get(created["id"]), before)

    def test_ids_are_never_reused_and_sku_is_unique(self):
        store = ProductStore()
        first = store.create(**product_fields())
        with self.assertRaises(ApiError) as error:
            store.create(**product_fields(name="Duplicate"))
        self.assertEqual(error.exception.code, "duplicate_sku")
        self.assertEqual(error.exception.details[0]["field"], "sku")
        self.assertTrue(store.delete(first["id"]))
        self.assertEqual(store.create(**product_fields(sku="ITEM-2"))["id"], 2)

    def test_copies_isolate_nested_tags_and_updates_keep_sku(self):
        store = ProductStore()
        created = store.create(**product_fields(tags=["sale"]))
        created["tags"].append("external")
        fetched = store.get(1)
        fetched["tags"].append("also-external")
        self.assertEqual(store.get(1)["tags"], ["sale"])
        updated = store.update(1, {"name": "Changed"})
        self.assertEqual(updated["sku"], "ITEM-1")
        self.assertNotEqual(updated["updated_at"], updated["created_at"])

    def test_adjust_stock_race_never_goes_negative(self):
        store = ProductStore()
        store.create(**product_fields(stock=100))
        barrier = threading.Barrier(200)

        def remove_one(_):
            barrier.wait(timeout=10)
            try:
                return store.adjust_stock(1, -1)["stock"]
            except ApiError as error:
                self.assertEqual(error.code, "insufficient_stock")
                return None

        with ThreadPoolExecutor(max_workers=200) as executor:
            results = list(executor.map(remove_one, range(200)))
        successful_stocks = [stock for stock in results if stock is not None]
        self.assertEqual(len(successful_stocks), 100)
        self.assertTrue(all(stock >= 0 for stock in successful_stocks))
        self.assertGreaterEqual(store.get(1)["stock"], 0)

    def test_capacity_is_bounded_and_deleted_ids_are_not_reused(self):
        store = ProductStore()
        for index in range(MAX_PRODUCTS):
            store.create(**product_fields(sku=f"ITEM-{index + 1}"))
        with self.assertRaises(ApiError) as error:
            store.create(**product_fields(sku="OVERFLOW"))
        self.assertEqual(error.exception.code, "store_full")
        store.delete(1)
        self.assertEqual(store.create(**product_fields(sku="AFTER"))["id"], 501)

    def test_list_sorts_with_id_tie_break_and_categories_aggregate(self):
        store = ProductStore()
        store.create(**product_fields(name="B", price_cents=10, stock=1))
        store.create(**product_fields(sku="ITEM-2", name="A", price_cents=10))
        store.create(
            **product_fields(
                sku="ITEM-3", category="other", active=False, price_cents=0
            )
        )
        products, total = store.list(sort="-price_cents", limit=1, offset=1)
        self.assertEqual(total, 3)
        self.assertEqual(products[0]["id"], 2)
        self.assertEqual(
            store.categories(),
            [
                {
                    "category": "example",
                    "products": 2,
                    "active_products": 2,
                    "in_stock": 1,
                    "min_price_cents": 10,
                    "max_price_cents": 10,
                },
                {
                    "category": "other",
                    "products": 1,
                    "active_products": 0,
                    "in_stock": 0,
                    "min_price_cents": 0,
                    "max_price_cents": 0,
                },
            ],
        )

    def test_name_sort_uses_plain_case_sensitive_order_and_id_ties(self):
        store = ProductStore()
        for index, name in enumerate(("a", "A", "A"), start=1):
            store.create(**product_fields(sku=f"NAME-{index}", name=name))

        ascending, _ = store.list(sort="name", limit=3)
        descending, _ = store.list(sort="-name", limit=3)
        self.assertEqual([item["id"] for item in ascending], [2, 3, 1])
        self.assertEqual([item["id"] for item in descending], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
