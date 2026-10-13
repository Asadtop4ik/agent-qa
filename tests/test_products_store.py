"""Tests for product validation and thread-safe in-memory storage."""

from concurrent.futures import ThreadPoolExecutor
import unittest

from agent_qa.products import ProductError, ProductStore


def product(sku: str, **changes):
    return {
        "sku": sku,
        "name": "Test product",
        "category": "test",
        "price_cents": 0,
        **changes,
    }


class ProductStoreTests(unittest.TestCase):
    def test_unique_sku_defaults_and_never_reused_ids(self):
        store = ProductStore()
        created = store.create(**product("TEST-ONE"))
        self.assertEqual(
            (created["stock"], created["tags"], created["active"]), (0, [], True)
        )
        with self.assertRaises(ProductError) as duplicate:
            store.create(**product("TEST-ONE"))
        self.assertEqual(duplicate.exception.code, "duplicate_sku")
        self.assertEqual(duplicate.exception.details[0]["field"], "sku")
        self.assertEqual(store.patch(created["id"], name="Changed")["sku"], "TEST-ONE")
        with self.assertRaises(ProductError) as immutable:
            store.patch(created["id"], sku="TEST-TWO")
        self.assertEqual(immutable.exception.details[0]["message"], "Cannot be changed")
        self.assertTrue(store.delete(created["id"]))
        self.assertEqual(store.create(**product("TEST-THREE"))["id"], 2)

    def test_capacity_and_field_validation(self):
        store = ProductStore()
        for index in range(500):
            store.create(**product(f"ITEM-{index:03d}"))
        with self.assertRaises(ProductError) as full:
            store.create(**product("ITEM-OVERFLOW"))
        self.assertEqual(full.exception.code, "store_full")

        invalid = (
            (product("A"), "sku"),
            (product("ITEM-INVALID\n"), "sku"),
            (product("ITEM-INVALID", name=" "), "name"),
            (product("ITEM-INVALID", category="UPPER"), "category"),
            (product("ITEM-INVALID", category="test\n"), "category"),
            (product("ITEM-INVALID", price_cents=True), "price_cents"),
            (product("ITEM-INVALID", stock=1_000_001), "stock"),
            (product("ITEM-INVALID", tags=["repeat", "repeat"]), "tags"),
            (product("ITEM-INVALID", tags=["valid\n"]), "tags[0]"),
        )
        for payload, field in invalid:
            with self.subTest(field=field), self.assertRaises(ProductError) as error:
                ProductStore().create(**payload)
            self.assertEqual(error.exception.code, "validation_error")
            self.assertIn(
                field, [detail["field"] for detail in error.exception.details]
            )

    def test_concurrent_stock_adjustments_never_go_below_zero(self):
        store = ProductStore()
        created = store.create(**product("RACE-ITEM", stock=100))

        def decrement(_):
            try:
                store.adjust_stock(created["id"], -1)
                return "ok"
            except ProductError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=200) as executor:
            results = list(executor.map(decrement, range(200)))
        self.assertEqual(store.get(created["id"])["stock"], 0)
        self.assertEqual(results.count("ok"), 100)
        self.assertEqual(results.count("insufficient_stock"), 100)

    def test_returned_tags_are_defensive_copies(self):
        store = ProductStore()
        supplied_tags = ["tag"]
        created = store.create(**product("COPY-ITEM", tags=supplied_tags))
        supplied_tags.append("outside")
        self.assertEqual(store.get(created["id"])["tags"], ["tag"])

        patch_tags = ["patched"]
        store.patch(created["id"], tags=patch_tags)
        patch_tags.append("outside")
        self.assertEqual(store.get(created["id"])["tags"], ["patched"])

        fetched = store.get(created["id"])
        fetched["tags"].append("outside")
        listed, _ = store.list()
        listed[0]["tags"].append("outside")
        adjusted = store.adjust_stock(created["id"], 1)
        adjusted["tags"].append("outside")
        self.assertEqual(store.get(created["id"])["tags"], ["patched"])

    def test_boolean_ids_do_not_alias_product_one(self):
        store = ProductStore()
        created = store.create(**product("BOOL-ID", stock=5))

        self.assertIsNone(store.patch(True, name="Changed"))
        self.assertIsNone(store.adjust_stock(True, -1))
        self.assertFalse(store.delete(True))
        self.assertEqual(store.get(created["id"])["name"], "Test product")
        self.assertEqual(store.get(created["id"])["stock"], 5)


if __name__ == "__main__":
    unittest.main()
