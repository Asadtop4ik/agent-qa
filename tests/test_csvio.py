"""Focused tests for CSV encoding, parsing, and type conversion."""

import unittest
from unittest.mock import patch

from agent_qa.csvio import (
    ORDER_COLUMNS,
    PRODUCT_COLUMNS,
    coerce_row,
    parse_csv,
    render_csv,
)
from agent_qa.errors import ApiError
from agent_qa.orders import OrderStore
from agent_qa.products import ProductStore, validate_create as validate_product_create


class CsvIoTests(unittest.TestCase):
    def test_render_uses_minimal_quoting_crlf_and_handles_newlines(self):
        result = render_csv(
            ("name", "value"),
            [{"name": "a,b", "value": 'say "hi"\nnow'}],
            text_columns={"name", "value"},
        )
        self.assertEqual(result, 'name,value\r\n"a,b","say ""hi""\nnow"\r\n')

    def test_formula_sanitizing_round_trips_product_text(self):
        product = {
            "id": 4,
            "sku": "A-1",
            "name": "=SUM(A1:A2)",
            "category": "demo",
            "price_cents": 50,
            "stock": 2,
            "tags": ["one", "two"],
            "active": True,
            "created_at": "now",
            "updated_at": "now",
        }
        csv_text = render_csv(
            PRODUCT_COLUMNS,
            [product],
            text_columns={"sku", "name", "category", "created_at", "updated_at"},
        )
        self.assertIn("'=SUM(A1:A2)", csv_text)
        line, values = parse_csv(csv_text, "products")[0]
        self.assertEqual(line, 2)
        self.assertEqual(
            coerce_row("products", values),
            {
                "sku": "A-1",
                "name": "=SUM(A1:A2)",
                "category": "demo",
                "price_cents": 50,
                "stock": 2,
                "tags": ["one", "two"],
                "active": True,
            },
        )

    def test_formula_prefix_variants_and_literal_apostrophe_round_trip(self):
        values = [
            "=x",
            "+x",
            "-x",
            "@x",
            "\tx",
            "\rx",
            "'=x",
            "''=x",
            "'''=x",
        ]
        products = [
            {"sku": f"A-{index}", "name": value, "category": "x", "price_cents": 1}
            for index, value in enumerate(values)
        ]
        csv_text = render_csv(
            ("sku", "name", "category", "price_cents"),
            products,
            {"sku", "name", "category"},
        )
        rows = parse_csv(csv_text, "products")
        self.assertEqual(
            [row[1]["name"] for row in rows],
            [
                "'=x",
                "'+x",
                "'-x",
                "'@x",
                "'\tx",
                "'\rx",
                "''=x",
                "'''=x",
                "''''=x",
            ],
        )
        self.assertEqual(
            [coerce_row("products", row[1])["name"] for row in rows], values
        )

    def test_bom_readonly_columns_and_order_coercion(self):
        parsed = parse_csv(
            "\ufeffid,customer_id,total_cents,status,items_count,created_at\r\n"
            "7,c-1,100,shipped,2,now\r\n",
            "orders",
        )
        self.assertEqual(
            coerce_row("orders", parsed[0][1]),
            {"customer_id": "c-1", "total_cents": 100},
        )
        self.assertEqual(ORDER_COLUMNS[0], "id")

    def test_structure_errors_are_invalid_csv_with_header_or_body_field(self):
        cases = (
            ("sku,sku,name,category,price_cents\na,a,n,c,1\n", "header"),
            ("sku,name,category,price_cents\na,n,c\n", "body"),
            ("sku,name,category,price_cents\n", "body"),
            ('sku,name,category,price_cents\na,n,c,1\n"bad', "body"),
        )
        for text, field in cases:
            with self.subTest(text=text), self.assertRaises(ApiError) as caught:
                parse_csv(text, "products")
            self.assertEqual(caught.exception.code, "invalid_csv")
            self.assertEqual(caught.exception.details[0]["field"], field)

    def test_csv_row_count_limit_allows_200_and_rejects_201(self):
        header = "sku,name,category,price_cents\n"
        row = "AB,item,x,1\n"
        self.assertEqual(len(parse_csv(header + row * 200, "products")), 200)
        with self.assertRaises(ApiError) as caught:
            parse_csv(header + row * 201, "products")
        self.assertEqual(caught.exception.details[0]["field"], "body")

    def test_multiline_records_use_logical_record_numbers(self):
        text = (
            "sku,name,category,price_cents\r\n"
            'AB-1,"first\r\nsecond",x,1\r\n'
            "AB-2,third,x,1\r\n"
        )
        self.assertEqual(
            [line for line, _values in parse_csv(text, "products")], [2, 3]
        )

    def test_numeric_coercion_bounds_huge_inputs_before_conversion(self):
        self.assertEqual(
            coerce_row("orders", {"customer_id": "c-1", "total_cents": "-5"})[
                "total_cents"
            ],
            -5,
        )
        huge = coerce_row(
            "products",
            {
                "sku": "AB",
                "name": "x",
                "category": "x",
                "price_cents": "9" * 10000,
            },
        )
        self.assertEqual(huge["price_cents"], 100_000_001)

    def test_import_uses_creation_validator_messages_for_multiple_errors(self):
        store = ProductStore()
        raw = {
            "sku": "AB-1",
            "name": "",
            "category": "bad#",
            "price_cents": "invalid",
            "stock": "9" * 10000,
            "active": "sometimes",
        }
        with self.assertRaises(ApiError) as expected:
            validate_product_create(coerce_row("products", raw))

        status, report = store.import_rows([(2, raw)], mode="validate")
        self.assertEqual(status, 200)
        self.assertEqual(
            report["errors"],
            [{"line": 2, **detail} for detail in expected.exception.details],
        )
        self.assertEqual(report["failed"], 1)

    def test_product_import_policies_and_existing_duplicate(self):
        store = ProductStore()
        rows = [
            (2, {"sku": "AB-1", "name": "valid", "category": "x", "price_cents": "1"}),
            (3, {"sku": "AB-2", "name": "", "category": "x", "price_cents": "1"}),
        ]
        status, report = store.import_rows(rows, mode="validate")
        self.assertEqual(status, 200)
        self.assertEqual(report["created"], 0)
        self.assertFalse(report["applied"])
        self.assertEqual(store._products, {})

        status, report = store.import_rows(rows, mode="apply", on_error="abort")
        self.assertEqual(status, 422)
        self.assertEqual(store._products, {})
        self.assertEqual(report["errors"][0]["line"], 3)

        status, report = store.import_rows(rows, mode="apply", on_error="skip")
        self.assertEqual(status, 201)
        self.assertEqual(report["created"], 1)
        self.assertTrue(report["applied"])
        self.assertEqual(store._products[1]["sku"], "AB-1")

        status, report = store.import_rows([rows[0]], mode="validate")
        self.assertEqual(status, 200)
        self.assertEqual(
            report["errors"],
            [{"line": 2, "field": "sku", "message": "SKU already exists"}],
        )

    def test_duplicate_sku_is_reported_after_an_invalid_row(self):
        store = ProductStore()
        rows = [
            (
                2,
                {
                    "sku": "AB-1",
                    "name": "",
                    "category": "x",
                    "price_cents": "1",
                },
            ),
            (
                3,
                {
                    "sku": "AB-1",
                    "name": "valid",
                    "category": "x",
                    "price_cents": "1",
                },
            ),
        ]
        status, report = store.import_rows(rows, on_error="skip")
        self.assertEqual(status, 422)
        self.assertEqual(report["failed"], 2)
        self.assertEqual(
            report["errors"],
            [
                {
                    "line": 2,
                    "field": "name",
                    "message": "Must contain 1 to 120 characters",
                },
                {
                    "line": 3,
                    "field": "sku",
                    "message": "Duplicate sku in file",
                },
            ],
        )

    def test_unexpected_product_import_failure_rolls_back_records_and_ids(self):
        store = ProductStore()
        rows = [
            (
                2,
                {
                    "sku": "AB-1",
                    "name": "one",
                    "category": "x",
                    "price_cents": "1",
                },
            ),
            (
                3,
                {
                    "sku": "AB-2",
                    "name": "two",
                    "category": "x",
                    "price_cents": "1",
                },
            ),
        ]
        with patch("agent_qa.products._now", side_effect=["now", RuntimeError("fail")]):
            with self.assertLogs("agent_qa.products", level="ERROR"):
                with self.assertRaises(RuntimeError):
                    store.import_rows(rows)
        self.assertEqual(store._products, {})
        self.assertEqual(store._next_id, 1)

    def test_order_import_uses_legacy_create_shape(self):
        store = OrderStore()
        status, report = store.import_rows(
            [
                (
                    2,
                    {
                        "id": "8",
                        "customer_id": "c-1",
                        "total_cents": "100",
                        "status": "paid",
                    },
                )
            ]
        )
        self.assertEqual(status, 201)
        self.assertEqual(report["created"], 1)
        self.assertEqual(store._orders[1]["status"], "new")
        self.assertEqual(store._orders[1]["total_cents"], 100)

    def test_export_filters_and_order_round_trip(self):
        products = ProductStore()
        products.create(
            sku="A-1",
            name="A product",
            category="x",
            price_cents=50,
            tags=["needle"],
        )
        self.assertEqual(
            [row["sku"] for row in products.export_rows({"q": "needle"})],
            ["A-1"],
        )
        self.assertEqual(products.export_rows({"q": "missing"}), [])

        source = OrderStore()
        source.create("customer-1", 100)
        source._orders[1]["status"] = "paid"
        exported = render_csv(
            ORDER_COLUMNS,
            source.export_rows({"status": "paid", "customer_id": "customer-1"}),
            {"customer_id", "status", "created_at"},
        )
        target = OrderStore()
        status, report = target.import_rows(parse_csv(exported, "orders"))
        self.assertEqual(status, 201)
        self.assertEqual(report["created"], 1)
        self.assertEqual(target._orders[1]["customer_id"], "customer-1")
        self.assertEqual(target._orders[1]["status"], "new")


if __name__ == "__main__":
    unittest.main()
