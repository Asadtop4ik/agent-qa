"""Tests for the HTTP-independent search query DSL."""

import unittest

from agent_qa.errors import ApiError
from agent_qa.searchdsl import (
    compile_predicate,
    count_terms,
    normalize,
    parse,
    tokenize,
)


class SearchDslTests(unittest.TestCase):
    def test_precedence_and_implicit_and(self):
        ast = parse("status:paid total_cents>=1000 OR NOT status:cancelled", "orders")
        self.assertEqual(
            ast,
            {
                "op": "or",
                "args": [
                    {
                        "op": "and",
                        "args": [
                            {"field": "status", "op": "=", "value": "paid"},
                            {"field": "total_cents", "op": ">=", "value": 1000},
                        ],
                    },
                    {
                        "op": "not",
                        "arg": {"field": "status", "op": "=", "value": "cancelled"},
                    },
                ],
            },
        )
        self.assertEqual(
            normalize(ast),
            "status = paid AND total_cents >= 1000 OR NOT status = cancelled",
        )

    def test_normalization_parentheses_and_round_trip(self):
        queries = [
            "status=paid OR (status=cancelled AND total_cents<30)",
            "NOT (status=paid OR status=shipped)",
            'customer_id="A AND B"',
            'customer_id="say \\"hi\\""',
        ]
        for query in queries:
            with self.subTest(query=query):
                normalized = normalize(parse(query, "orders"))
                self.assertEqual(parse(normalized, "orders"), parse(query, "orders"))

    def test_in_values_are_typed(self):
        ast = parse("id IN (1, 2, 3)", "orders")
        self.assertEqual(ast["value"], [1, 2, 3])
        self.assertEqual(normalize(ast), "id IN (1, 2, 3)")

    def test_table_of_typed_terms_and_operators(self):
        cases = [
            ("id=1", "orders", "id = 1"),
            ("id:2", "orders", "id = 2"),
            ("id!=3", "orders", "id != 3"),
            ("id>4", "orders", "id > 4"),
            ("id>=5", "orders", "id >= 5"),
            ("id<6", "orders", "id < 6"),
            ("id<=7", "orders", "id <= 7"),
            ("customer_id=acme", "orders", "customer_id = acme"),
            ("customer_id~AC", "orders", "customer_id ~ AC"),
            ("status=new", "orders", "status = new"),
            ("created_at=2024-01-01", "orders", "created_at = 2024-01-01"),
            ("items_count=0", "orders", "items_count = 0"),
            ("product_id IN (1,2)", "orders", "product_id IN (1, 2)"),
            ("sku=AB-1", "orders", "sku = AB-1"),
            ("id=1", "products", "id = 1"),
            ("sku=AB-1", "products", "sku = AB-1"),
            ("name=widget", "products", "name = widget"),
            ("category=tools", "products", "category = tools"),
            ("price_cents=100", "products", "price_cents = 100"),
            ("stock=0", "products", "stock = 0"),
            ("tags IN (sale,new)", "products", "tags IN (sale, new)"),
            ("active=true", "products", "active = true"),
            ("created_at>=2024-01-01", "products", "created_at >= 2024-01-01"),
            ("id=1 AND id=2", "orders", "id = 1 AND id = 2"),
            ("id=1 OR id=2", "orders", "id = 1 OR id = 2"),
            ("NOT id=1", "orders", "NOT id = 1"),
            ("id=1 id=2", "orders", "id = 1 AND id = 2"),
            ("(id=1)", "orders", "id = 1"),
            ("id=1 OR id=2 AND id=3", "orders", "id = 1 OR id = 2 AND id = 3"),
            ("id=1 AND (id=2 OR id=3)", "orders", "id = 1 AND (id = 2 OR id = 3)"),
            ('name="two words"', "products", 'name = "two words"'),
        ]
        for query, resource, expected in cases:
            with self.subTest(query=query, resource=resource):
                ast = parse(query, resource)
                self.assertEqual(normalize(ast), expected)
                self.assertEqual(parse(expected, resource), ast)

    def test_token_positions_and_escapes(self):
        tokens = tokenize('name="a\\\\b\\"c"')
        self.assertEqual(
            [(item.kind, item.value, item.position) for item in tokens],
            [("word", "name", 0), ("operator", "=", 4), ("value", 'a\\b"c', 5)],
        )

    def test_invalid_operator_and_parenthesis_positions(self):
        cases = [
            ("id!1", "orders", "Unexpected token '!' at position 2", "2"),
            ("id IN (1,2", "orders", "Expected ')' at position 10", "10"),
            ("(id=1, id=2)", "orders", "Expected ')' at position 5", "5"),
        ]
        for query, resource, message, position in cases:
            with self.subTest(query=query), self.assertRaises(ApiError) as caught:
                parse(query, resource)
            self.assertEqual(caught.exception.message, message)
            self.assertEqual(caught.exception.details[0]["position"], position)

    def test_date_in_predicate_handles_value_list_and_overflow(self):
        query = 'created_at="0001-01-01T00:00:00+01:00"'
        with self.assertRaises(ApiError) as caught:
            parse(query, "orders")
        self.assertEqual(
            caught.exception.message,
            "Expected date for field created_at at position 11",
        )
        with self.assertRaises(ApiError) as invalid_iso:
            parse("created_at=20240101T000000Z", "orders")
        self.assertEqual(
            invalid_iso.exception.message,
            "Expected date for field created_at at position 11",
        )
        predicate = compile_predicate(
            parse("created_at IN (2024-01-01,2024-01-02)", "orders"), "orders"
        )
        self.assertTrue(predicate({"created_at": "2024-01-01T00:00:00Z"}))

    def test_predicate_operator_matrix(self):
        numeric_row = {"id": 5}
        numeric_cases = [
            ("id=5", True),
            ("id!=5", False),
            ("id>4", True),
            ("id>=5", True),
            ("id<6", True),
            ("id<=5", True),
            ("id IN (4,5)", True),
        ]
        for query, expected in numeric_cases:
            with self.subTest(query=query):
                self.assertEqual(
                    compile_predicate(parse(query, "orders"), "orders")(numeric_row),
                    expected,
                )
        string_row = {"customer_id": "Acme"}
        string_cases = [
            ("customer_id:Acme", True),
            ("customer_id!=acme", True),
            ("customer_id~AC", True),
            ("customer_id IN (Other,Acme)", True),
        ]
        for query, expected in string_cases:
            with self.subTest(query=query):
                self.assertEqual(
                    compile_predicate(parse(query, "orders"), "orders")(string_row),
                    expected,
                )

    def test_rejects_raw_control_characters_in_quoted_values(self):
        with self.assertRaises(ApiError) as caught:
            parse('name="a\nb"', "products")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.code, "invalid_search_query")

    def test_predicate_composition_case_and_collection_semantics(self):
        row = {
            "id": 1,
            "customer_id": "Acme",
            "status": "paid",
            "items": [
                {"product_id": 3, "sku": "AB-3"},
                {"product_id": 4, "sku": "CD-4"},
            ],
        }
        self.assertFalse(
            compile_predicate(parse("customer_id=acme", "orders"), "orders")(row)
        )
        self.assertTrue(
            compile_predicate(parse("customer_id~ACME", "orders"), "orders")(row)
        )
        self.assertTrue(
            compile_predicate(parse("product_id IN (2,3)", "orders"), "orders")(row)
        )
        self.assertFalse(compile_predicate(parse("sku!=AB-3", "orders"), "orders")(row))
        combined = compile_predicate(
            parse("NOT status=cancelled AND (id=1 OR id=2)", "orders"), "orders"
        )
        self.assertTrue(combined(row))

    def test_all_complexity_limits_allow_exact_boundary(self):
        self.assertEqual(parse("id=1" + " " * 496, "orders")["field"], "id")
        nested = "(" * 8 + "id=1" + ")" * 8
        self.assertEqual(parse(nested, "orders")["field"], "id")
        self.assertEqual(count_terms(parse(" AND ".join(["id=1"] * 20), "orders")), 20)
        self.assertEqual(count_terms(parse("NOT " * 8 + "id=1", "orders")), 1)
        with self.assertRaises(ApiError) as too_deep_not:
            parse("NOT " * 9 + "id=1", "orders")
        self.assertEqual(
            too_deep_not.exception.message,
            "Query is nested too deeply (max 8)",
        )

    def test_predicates(self):
        order = {
            "id": 7,
            "customer_id": "Acme",
            "total_cents": 1200,
            "status": "paid",
            "created_at": "2024-01-02T12:00:00Z",
            "items": [{"product_id": 3, "sku": "AB-3", "quantity": 1}],
        }
        product = {
            "id": 3,
            "sku": "AB-3",
            "name": "Blue Widget",
            "category": "tools",
            "price_cents": 500,
            "stock": 5,
            "tags": ["sale"],
            "active": True,
            "created_at": "2024-01-02T12:00:00Z",
        }
        self.assertTrue(
            compile_predicate(parse("product_id=3", "orders"), "orders")(order)
        )
        self.assertFalse(
            compile_predicate(parse("sku!=AB-3", "orders"), "orders")(order)
        )
        self.assertTrue(
            compile_predicate(parse('name~"WIDG"', "products"), "products")(product)
        )
        self.assertTrue(
            compile_predicate(parse("active=true", "products"), "products")(product)
        )
        self.assertTrue(
            compile_predicate(parse("created_at>=2024-01-02", "orders"), "orders")(
                order
            )
        )

    def test_known_invalid_inputs_have_positioned_details(self):
        cases = [
            ("", "Query must not be empty", 0),
            ("total_cents=", "Expected value at position 12", 12),
            ("(status=paid", "Expected ')' at position 12", 12),
            ("status=paid ?", "Unexpected token '?' at position 12", 12),
            (
                "totl_cents=3",
                "Unknown field 'totl_cents'. Did you mean 'total_cents'?",
                0,
            ),
            (
                "total_cents=abc",
                "Expected integer for field total_cents at position 12",
                12,
            ),
            ("status=bogus", "Unknown value 'bogus' for field status at position 7", 7),
            (
                "status>paid",
                "Operator > is not supported for field status at position 6",
                6,
            ),
        ]
        for query, message, position in cases:
            with self.subTest(query=query), self.assertRaises(ApiError) as caught:
                parse(query, "orders")
            self.assertEqual(caught.exception.status, 400)
            self.assertEqual(caught.exception.code, "invalid_search_query")
            self.assertEqual(
                caught.exception.details,
                [{"field": "q", "message": message, "position": str(position)}],
            )

    def test_boolean_date_and_operator_errors_keep_token_positions(self):
        cases = [
            (
                "active=yes",
                "products",
                "Expected boolean for field active at position 7",
                "7",
            ),
            (
                "created_at=bogus",
                "orders",
                "Expected date for field created_at at position 11",
                "11",
            ),
            (
                "active~true",
                "products",
                "Operator ~ is not supported for field active at position 6",
                "6",
            ),
            (
                "name>widget",
                "products",
                "Operator > is not supported for field name at position 4",
                "4",
            ),
            (
                "price_cents~nope",
                "products",
                "Operator ~ is not supported for field price_cents at position 11",
                "11",
            ),
        ]
        for query, resource, message, position in cases:
            with self.subTest(query=query), self.assertRaises(ApiError) as caught:
                parse(query, resource)
            self.assertEqual(
                caught.exception.details,
                [{"field": "q", "message": message, "position": position}],
            )

    def test_limits_and_term_count(self):
        with self.assertRaises(ApiError) as too_long:
            parse(" " * 501, "orders")
        self.assertEqual(too_long.exception.message, "Query is too long (max 500)")
        with self.assertRaises(ApiError) as too_deep:
            parse("(" * 9 + "id=1" + ")" * 9, "orders")
        self.assertEqual(
            too_deep.exception.message, "Query is nested too deeply (max 8)"
        )
        with self.assertRaises(ApiError) as too_many:
            parse(" AND ".join(["id=1"] * 21), "orders")
        self.assertEqual(too_many.exception.message, "Too many terms (max 20)")
        self.assertEqual(count_terms(parse("id=1 OR id=2", "orders")), 2)


if __name__ == "__main__":
    unittest.main()
