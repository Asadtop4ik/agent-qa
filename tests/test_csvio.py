"""Focused tests for CSV parsing, rendering, and store imports."""

import unittest
from unittest.mock import patch

from agent_qa.csvio import (
    CsvStructureError,
    coerce_int,
    import_orders_csv,
    import_products_csv,
    parse_csv,
    render_csv,
)
from agent_qa.errors import ApiError
from agent_qa.orders import OrderStore
from agent_qa.products import ProductStore


PRODUCT_HEADERS = ("sku", "name", "category", "price_cents", "stock", "tags", "active")


class CsvPureFunctionTests(unittest.TestCase):
    def test_render_uses_crlf_minimal_quoting_and_formula_protection(self):
        rendered = render_csv(
            ("name", "note", "price"),
            [{"name": "=SUM(A1)", "note": 'line 1, "line 2"\nnext', "price": -5}],
            text_columns={"name", "note"},
        )
        self.assertEqual(
            rendered,
            'name,note,price\r\n\'=SUM(A1),"line 1, ""line 2""\nnext",-5\r\n',
        )

    def test_render_sanitizes_all_text_prefixes_but_not_numbers(self):
        dangerous = ("=x", "+x", "-x", "@x", "\tx", "\rx")
        rendered = render_csv(
            ("value", "number"),
            [{"value": value, "number": -5} for value in dangerous],
            text_columns={"value"},
        )
        rows = parse_csv(rendered, ("value", "number"), ("value", "number"))
        self.assertEqual(
            [row["value"] for row in rows], ["'" + value for value in dangerous]
        )
        self.assertEqual([row["number"] for row in rows], ["-5"] * len(dangerous))

    def test_render_keeps_header_when_no_rows_match(self):
        self.assertEqual(render_csv(("id", "name"), []), "id,name\r\n")

    def test_parse_removes_bom_and_returns_records_with_embedded_newline(self):
        rows = parse_csv(
            (
                "\ufeffsku,name,category,price_cents\r\n"
                'A-1,"line one\nline two",food,4\r\n'
            ),
            ("sku", "name", "category", "price_cents"),
            ("sku", "name", "category", "price_cents"),
        )
        self.assertEqual(
            rows,
            [
                {
                    "sku": "A-1",
                    "name": "line one\nline two",
                    "category": "food",
                    "price_cents": "4",
                }
            ],
        )

    def test_parse_rejects_duplicate_or_missing_headers_and_bad_rows(self):
        for text in (
            "sku,sku\na,b\n",
            "sku\na\n",
            "sku,name\na\n",
            'sku,name\na,"unclosed\n',
            'sku,name\na,b"c\n',
            'sku,name\na,"b"tail\n',
            "",
        ):
            with self.subTest(text=text):
                with self.assertRaises(CsvStructureError):
                    parse_csv(text, ("sku", "name"), ("sku", "name"))


class CsvImportTests(unittest.TestCase):
    def product_csv(self, *records):
        return ",".join(PRODUCT_HEADERS) + "\r\n" + "\r\n".join(records) + "\r\n"

    def test_product_validate_skip_abort_and_duplicate_sku_policies(self):
        store = ProductStore()
        data = self.product_csv(
            "GOOD-1,Good,food,100,2,one|two,true",
            "GOOD-1,Duplicate,food,100,2,,true",
            "BAD-1,Bad,food,nope,2,,true",
        )
        status, report = import_products_csv(store, data, "validate", "abort")
        self.assertEqual(status, 200)
        self.assertEqual(
            (report["rows"], report["created"], report["failed"]), (3, 0, 2)
        )
        self.assertFalse(report["applied"])
        self.assertEqual(store._products, {})
        self.assertEqual(
            report["errors"][0],
            {"line": 3, "field": "sku", "message": "Duplicate sku in file"},
        )

        status, report = import_products_csv(store, data, "apply", "abort")
        self.assertEqual((status, report["created"]), (422, 0))
        self.assertEqual(store._products, {})
        status, report = import_products_csv(store, data, "apply", "skip")
        self.assertEqual((status, report["created"], report["failed"]), (201, 1, 2))
        self.assertTrue(report["applied"])
        self.assertEqual(store.get(1)["tags"], ["one", "two"])

    def test_validate_reports_rows_exceeding_store_capacity(self):
        cases = (
            (
                ProductStore,
                lambda store: store.create(
                    sku="EXIST-1", name="Existing", category="food", price_cents=1
                ),
                import_products_csv,
                (
                    self.product_csv("NEW-1,New,food,100,0,,true"),
                    self.product_csv(
                        "NEW-1,New,food,100,0,,true",
                        "NEW-2,Also new,food,200,0,,true",
                    ),
                ),
                "Product store is full",
            ),
            (
                OrderStore,
                lambda store: store.create("existing", 1),
                import_orders_csv,
                (
                    "customer_id,total_cents\r\nnew,100\r\n",
                    "customer_id,total_cents\r\nnew,100\r\nalso-new,200\r\n",
                ),
                "Order store is full",
            ),
        )
        for store_type, seed, importer, data_by_capacity, full_message in cases:
            for capacity, data, error_line in (
                (1, data_by_capacity[0], 2),
                (2, data_by_capacity[1], 3),
            ):
                with self.subTest(store=store_type.__name__, capacity=capacity):
                    store = store_type(capacity=capacity)
                    seed(store)
                    existing = store.get(1)
                    next_id = store._next_id

                    validate_status, validate_report = importer(
                        store, data, "validate", "abort"
                    )
                    apply_status, apply_report = importer(store, data, "apply", "abort")

                    expected_error = {
                        "line": error_line,
                        "field": "body",
                        "message": full_message,
                    }
                    self.assertEqual(validate_status, 200)
                    self.assertEqual(
                        (validate_report["created"], validate_report["failed"]),
                        (0, 1),
                    )
                    self.assertFalse(validate_report["applied"])
                    self.assertEqual(validate_report["errors"], [expected_error])
                    self.assertEqual((apply_status, apply_report["created"]), (422, 0))
                    self.assertEqual(apply_report["errors"], validate_report["errors"])
                    self.assertEqual(store.get(1), existing)
                    self.assertEqual(store._next_id, next_id)

    def test_failed_counts_rows_when_validation_returns_multiple_field_errors(self):
        store = ProductStore()
        bad_row = "BAD-1,Bad,Invalid Category,999999999,0,,true"
        _status, report = import_products_csv(
            store, self.product_csv(bad_row), "validate", "skip"
        )
        self.assertEqual(report["failed"], 1)
        self.assertEqual(len(report["errors"]), 2)
        self.assertEqual({error["line"] for error in report["errors"]}, {2})

    def test_duplicate_sku_is_reported_even_when_first_occurrence_is_invalid(self):
        store = ProductStore()
        data = self.product_csv(
            "DUP-1,Invalid,food,bad,0,,true",
            "DUP-1,Valid,food,1,0,,true",
        )
        _status, report = import_products_csv(store, data, "validate", "skip")
        self.assertEqual(report["failed"], 2)
        self.assertEqual(
            report["errors"],
            [
                {
                    "line": 2,
                    "field": "price_cents",
                    "message": "Must be an integer",
                },
                {"line": 3, "field": "sku", "message": "Duplicate sku in file"},
            ],
        )

    def test_csv_validation_messages_match_normal_create_validation(self):
        payload = {
            "sku": "BAD-1",
            "name": "Bad",
            "category": "food",
            "price_cents": 100_000_001,
        }
        with self.assertRaises(ApiError) as expected:
            ProductStore().create(**payload)
        data = self.product_csv("BAD-1,Bad,food,100000001,0,,true")
        _status, report = import_products_csv(ProductStore(), data, "validate", "skip")
        self.assertEqual(
            report["errors"],
            [{"line": 2, **expected.exception.details[0]}],
        )

    def test_huge_integer_text_is_bounded_without_throwing(self):
        huge = "9" * 5000
        self.assertEqual(coerce_int(huge), huge)
        _status, report = import_products_csv(
            ProductStore(),
            self.product_csv(f"BIG-1,Big,food,{huge},0,,true"),
            "validate",
            "skip",
        )
        self.assertEqual(report["failed"], 1)

    def test_unexpected_product_transaction_error_rolls_back_and_logs(self):
        store = ProductStore()
        original_create = store.create
        calls = 0

        def fail_second(**fields):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic failure")
            return original_create(**fields)

        data = self.product_csv(
            "GOOD-1,Good,food,1,0,,true", "GOOD-2,Good,food,1,0,,true"
        )
        with self.assertLogs("agent_qa.bulk", level="ERROR"):
            with patch.object(store, "create", side_effect=fail_second):
                with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                    import_products_csv(store, data, "apply", "skip")
        self.assertEqual(store._products, {})
        self.assertEqual(store._next_id, 1)

    def test_existing_product_sku_is_a_row_error(self):
        store = ProductStore()
        store.create(sku="EXIST-1", name="Existing", category="food", price_cents=1)
        status, report = import_products_csv(
            store, self.product_csv("EXIST-1,Other,food,1,0,,true"), "apply", "skip"
        )
        self.assertEqual(status, 422)
        self.assertEqual(
            report["errors"],
            [{"line": 2, "field": "sku", "message": "SKU already exists"}],
        )
        self.assertEqual(len(store._products), 1)

    def test_multiline_records_keep_logical_record_numbers(self):
        store = ProductStore()
        data = self.product_csv(
            'GOOD-1,"Good\nname",food,1,0,,true',
            "BAD-1,Bad,food,not-an-int,0,,true",
        )
        status, report = import_products_csv(store, data, "validate", "skip")
        self.assertEqual(status, 200)
        self.assertEqual(report["errors"][0]["line"], 3)

    def test_order_import_and_order_exports_round_trip_values(self):
        source = OrderStore()
        source.create("customer-a", 245)
        exported = source.export_csv()
        data = render_csv(
            ("customer_id", "total_cents"),
            exported,
            text_columns={"customer_id"},
        )
        store = OrderStore()
        status, report = import_orders_csv(store, data)
        self.assertEqual((status, report["created"], report["applied"]), (201, 1, True))
        order = store.get(1)
        self.assertEqual(
            (order["customer_id"], order["total_cents"]), ("customer-a", 245)
        )

    def test_product_export_import_round_trip_for_importable_columns(self):
        source = ProductStore()
        source.create(
            sku="ROUND-1",
            name="Round trip",
            category="food",
            price_cents=245,
            stock=3,
            tags=["fresh", "hot"],
            active=False,
        )
        exported = source.export_csv()
        import_headers = PRODUCT_HEADERS
        data = render_csv(
            import_headers,
            exported,
            text_columns={"sku", "name", "category", "tags"},
        )
        target = ProductStore()
        status, report = import_products_csv(target, data)
        self.assertEqual((status, report["created"]), (201, 1))
        result = target.get(1)
        self.assertEqual(
            tuple(result[name] for name in import_headers),
            ("ROUND-1", "Round trip", "food", 245, 3, ["fresh", "hot"], False),
        )

    def test_store_exports_apply_filters_and_expose_csv_columns(self):
        products = ProductStore()
        products.create(
            sku="ALPHA-1",
            name="=Unsafe",
            category="food",
            price_cents=2,
            stock=1,
            tags=["hot"],
        )
        products.create(
            sku="BETA-1",
            name="Safe",
            category="food",
            price_cents=3,
            active=False,
        )
        self.assertEqual(
            [row["sku"] for row in products.export_csv({"active": True})],
            ["ALPHA-1"],
        )
        self.assertEqual(
            [row["sku"] for row in products.export_csv({"q": "alpha"})],
            ["ALPHA-1"],
        )
        exported = products.export_csv()[0]
        self.assertEqual(
            (exported["tags"], exported["active"], exported["name"]),
            ("hot", "true", "=Unsafe"),
        )
        orders = OrderStore()
        orders.create("customer-a", 10)
        self.assertEqual(
            orders.export_csv({"customer_id": "customer-a"})[0]["items_count"], 0
        )

    def test_row_limit_and_invalid_csv_structure(self):
        store = ProductStore()
        with self.assertRaises(CsvStructureError):
            import_products_csv(store, "sku,name,category,price_cents\r\n")
        many = self.product_csv(*(f"A-{i},Name,food,1,0,,true" for i in range(201)))
        with self.assertRaises(CsvStructureError):
            import_products_csv(store, many)
        two_hundred = self.product_csv(
            *(f"A-{i},Name,food,1,0,,true" for i in range(200))
        )
        status, report = import_products_csv(ProductStore(), two_hundred)
        self.assertEqual((status, report["created"]), (201, 200))

    def test_order_export_observes_1000_row_cap(self):
        store = OrderStore()
        for index in range(1000):
            store.create(f"customer-{index}", index)
        self.assertEqual(len(store.export_csv()), 1000)


if __name__ == "__main__":
    unittest.main()
