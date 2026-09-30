"""Tests for the schema validator and generic query parameter helper."""

import unittest

from agent_qa.validation import validate, validate_query_params


class SchemaValidationTests(unittest.TestCase):
    def test_supports_each_schema_keyword(self):
        schema = {
            "type": "object",
            "required": ["name", "count", "enabled", "items"],
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 4,
                    "pattern": "^[a-z]+$",
                    "x-nonBlank": True,
                },
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3,
                    "enum": [1, 2, 3],
                },
                "enabled": {"type": "boolean"},
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 2,
                    "items": {
                        "type": "object",
                        "properties": {"qty": {"type": "number"}},
                        "required": ["qty"],
                        "additionalProperties": False,
                    },
                },
            },
            "minProperties": 4,
            "additionalProperties": {"type": "string"},
        }
        self.assertEqual(
            validate(
                schema,
                {
                    "name": "ab",
                    "count": 2,
                    "enabled": True,
                    "items": [{"qty": 1.5}],
                    "note": "ok",
                },
            ),
            [],
        )

    def test_reports_nested_paths_and_sorts_by_field(self):
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"qty": {"type": "integer"}},
                    },
                },
                "name": {"type": "string"},
            },
        }
        self.assertEqual(
            validate(schema, {"items": [{"qty": 1}, {"qty": "bad"}], "name": 2}),
            [
                {"field": "items[1].qty", "message": "Must be an integer"},
                {"field": "name", "message": "Must be a string"},
            ],
        )

    def test_boolean_is_neither_integer_nor_number(self):
        self.assertEqual(
            validate(
                {
                    "type": "object",
                    "properties": {
                        "integer": {"type": "integer"},
                        "number": {"type": "number"},
                    },
                },
                {"integer": True, "number": False},
            ),
            [
                {"field": "integer", "message": "Must be an integer"},
                {"field": "number", "message": "Must be a number"},
            ],
        )

    def test_order_list_items_use_and_validate_the_named_order_schema(self):
        from agent_qa.schemas import SCHEMAS

        self.assertIs(
            SCHEMAS["OrderList"]["properties"]["items"]["items"], SCHEMAS["Order"]
        )
        response = {
            "items": [
                {
                    "id": "1",
                    "customer_id": "customer",
                    "total_cents": 10,
                    "status": "new",
                    "created_at": "today",
                    "items": [],
                }
            ],
            "total": 1,
            "limit": 20,
            "offset": 0,
        }
        self.assertEqual(
            validate(SCHEMAS["OrderList"], response),
            [{"field": "items[0].id", "message": "Must be an integer"}],
        )

    def test_property_named_body_keeps_its_nested_path(self):
        schema = {
            "type": "object",
            "properties": {
                "body": {
                    "type": "object",
                    "required": ["qty"],
                    "properties": {"qty": {"type": "integer"}},
                }
            },
        }
        self.assertEqual(
            validate(schema, {"body": {"qty": "bad"}}),
            [{"field": "body.qty", "message": "Must be an integer"}],
        )
        self.assertEqual(
            validate(schema, {"body": {}}),
            [{"field": "body.qty", "message": "Required"}],
        )

    def test_validation_keywords_report_invalid_values(self):
        schema = {
            "type": "object",
            "required": ["required"],
            "minProperties": 9,
            "properties": {
                "integer": {
                    "type": "integer",
                    "minimum": 2,
                    "maximum": 4,
                },
                "enum": {"type": "string", "enum": ["a", "b"]},
                "short": {"type": "string", "minLength": 2},
                "long": {"type": "string", "maxLength": 3},
                "pattern": {"type": "string", "pattern": "^[a-z]+$"},
                "array": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 3,
                    "items": {"type": "boolean"},
                },
                "extra": {"type": "string"},
            },
            "additionalProperties": {"type": "integer"},
        }
        errors = validate(
            schema,
            {
                "integer": 9,
                "enum": "c",
                "short": "x",
                "long": "longer",
                "pattern": "NO",
                "array": [1],
                "extra": False,
                "other": "wrong type",
            },
        )
        self.assertEqual(
            errors,
            [
                {"field": "array", "message": "Must contain at least 2 items"},
                {"field": "array[0]", "message": "Must be a boolean"},
                {"field": "body", "message": "At least one field is required"},
                {"field": "enum", "message": "Must be one of: a, b"},
                {"field": "extra", "message": "Must be a string"},
                {"field": "integer", "message": "Must be between 2 and 4"},
                {"field": "long", "message": "Must contain at most 3 characters"},
                {"field": "other", "message": "Must be an integer"},
                {"field": "pattern", "message": "Must match the required pattern"},
                {"field": "required", "message": "Required"},
                {"field": "short", "message": "Must contain at least 2 characters"},
            ],
        )

    def test_order_schema_legacy_message_catalog(self):
        from agent_qa.schemas import SCHEMAS

        cases = (
            (
                {},
                [
                    {"field": "customer_id", "message": "Required"},
                    {
                        "field": "items",
                        "message": "Either items or total_cents is required",
                    },
                ],
            ),
            (
                {"customer_id": 1, "total_cents": "1"},
                [
                    {"field": "customer_id", "message": "Must be a string"},
                    {"field": "total_cents", "message": "Must be an integer"},
                ],
            ),
            (
                {"customer_id": "x" * 65, "total_cents": -1},
                [
                    {
                        "field": "customer_id",
                        "message": "Must contain 1 to 64 characters",
                    },
                    {
                        "field": "total_cents",
                        "message": "Must be between 0 and 100000000",
                    },
                ],
            ),
            (
                {"customer_id": " ", "total_cents": 0},
                [{"field": "customer_id", "message": "Must not be blank"}],
            ),
            (
                {"customer_id": "x", "total_cents": 100_000_001},
                [
                    {
                        "field": "total_cents",
                        "message": "Must be between 0 and 100000000",
                    }
                ],
            ),
            (
                {"customer_id": "x", "total_cents": 0, "z": 1},
                [{"field": "z", "message": "Unknown field"}],
            ),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(validate(SCHEMAS["CreateOrder"], payload), expected)

        self.assertEqual(
            validate(SCHEMAS["CreateOrder"], {"customer_id": "x"}),
            [
                {
                    "field": "items",
                    "message": "Either items or total_cents is required",
                }
            ],
        )
        self.assertEqual(
            validate(
                SCHEMAS["CreateOrder"],
                {"customer_id": "x", "items": [], "total_cents": 0},
            ),
            [
                {"field": "items", "message": "Cannot be combined with total_cents"},
                {"field": "items", "message": "Must contain at least 1 items"},
            ],
        )

    def test_order_item_quantity_is_bounded_even_for_huge_integers(self):
        from agent_qa.schemas import MAX_ORDER_ITEMS, MAX_ORDER_QUANTITY, SCHEMAS

        self.assertEqual(
            validate(
                SCHEMAS["CreateOrder"],
                {"customer_id": "x", "items": [{"product_id": 1, "quantity": 1}]},
            ),
            [],
        )
        self.assertEqual(
            validate(
                SCHEMAS["CreateOrder"],
                {
                    "customer_id": "x",
                    "items": [{"product_id": 1, "quantity": MAX_ORDER_QUANTITY + 1}],
                },
            ),
            [
                {
                    "field": "items[0].quantity",
                    "message": f"Must be between 1 and {MAX_ORDER_QUANTITY}",
                }
            ],
        )
        self.assertEqual(MAX_ORDER_ITEMS, 20)
        self.assertEqual(
            validate(
                SCHEMAS["CreateOrder"],
                {
                    "customer_id": "x",
                    "items": [{"product_id": 1, "quantity": 10**1000}],
                },
            ),
            [
                {
                    "field": "items[0].quantity",
                    "message": f"Must be between 1 and {MAX_ORDER_QUANTITY}",
                }
            ],
        )

        self.assertEqual(
            validate(SCHEMAS["UpdateOrder"], {"status": 12}),
            [
                {
                    "field": "status",
                    "message": "Must be one of: cancelled, new, paid, shipped",
                }
            ],
        )
        self.assertEqual(
            validate(SCHEMAS["UpdateOrder"], {}),
            [{"field": "body", "message": "At least one field is required"}],
        )
        self.assertEqual(
            validate(SCHEMAS["UpdateOrder"], []),
            [{"field": "body", "message": "Must be a JSON object"}],
        )

    def test_depth_and_invalid_patterns_are_reported_without_raising(self):
        self.assertTrue(validate({"type": "string", "pattern": "["}, "x"))
        value = None
        schema = {"type": "integer"}
        for _ in range(120):
            value = [value]
            schema = {"type": "array", "items": schema}
        self.assertEqual(
            validate(schema, value),
            [
                {
                    "field": "body" + "[0]" * 101,
                    "message": "Maximum nesting depth exceeded",
                }
            ],
        )

    def test_pattern_full_match_is_an_explicit_extension(self):
        self.assertEqual(validate({"type": "string", "pattern": "cat"}, "catalog"), [])
        self.assertTrue(
            validate(
                {"type": "string", "pattern": "cat", "x-fullMatch": True},
                "catalog",
            )
        )

    def test_product_pattern_and_nonzero_extensions_are_enforced(self):
        from agent_qa.schemas import SCHEMAS

        product = {
            "sku": "ITEM-1\n",
            "name": "Item",
            "category": "items",
            "price_cents": 0,
        }
        self.assertEqual(
            validate(SCHEMAS["CreateProduct"], product),
            [{"field": "sku", "message": "Must match the required pattern"}],
        )
        self.assertEqual(
            validate(SCHEMAS["AdjustStock"], {"delta": 0}),
            [{"field": "delta", "message": "Must not be zero"}],
        )


class QueryValidationTests(unittest.TestCase):
    PARAMETERS = [
        {
            "name": "limit",
            "in": "query",
            "schema": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 20,
            },
        },
        {
            "name": "active",
            "in": "query",
            "schema": {"type": "boolean", "default": False},
        },
        {
            "name": "status",
            "in": "query",
            "schema": {"type": "string", "enum": ["new", "paid"]},
        },
    ]

    def test_returns_typed_values_and_defaults(self):
        self.assertEqual(
            validate_query_params(
                self.PARAMETERS,
                [("limit", "2"), ("active", "true"), ("status", "paid")],
            ),
            {"limit": 2, "active": True, "status": "paid"},
        )
        self.assertEqual(
            validate_query_params(self.PARAMETERS, []),
            {"limit": 20, "active": False},
        )

    def test_unknown_duplicate_invalid_and_huge_values_raise_api_error(self):
        from agent_qa.errors import ApiError

        for query, field, message in (
            ([("x", "1")], "x", "Unsupported query parameter"),
            ([("limit", "1"), ("limit", "2")], "limit", "Parameter may appear once"),
            (
                [("limit", "999999999999999999999999999999")],
                "limit",
                "Must be an integer from 1 to 100",
            ),
        ):
            with self.subTest(query=query), self.assertRaises(ApiError) as error:
                validate_query_params(self.PARAMETERS, query)
            self.assertEqual(error.exception.status, 400)
            self.assertEqual(error.exception.code, "invalid_query")
            self.assertEqual(
                error.exception.details, [{"field": field, "message": message}]
            )

    def test_enum_is_checked_for_integer_and_boolean_queries(self):
        from agent_qa.errors import ApiError

        parameters = [
            {
                "name": "count",
                "in": "query",
                "schema": {"type": "integer", "enum": [1, 2]},
            },
            {
                "name": "active",
                "in": "query",
                "schema": {"type": "boolean", "enum": [True]},
            },
        ]
        with self.assertRaises(ApiError) as error:
            validate_query_params(parameters, [("count", "3"), ("active", "false")])
        self.assertEqual(
            error.exception.details,
            [
                {"field": "active", "message": "Must be one of: True"},
                {"field": "count", "message": "Must be one of: 1, 2"},
            ],
        )

    def test_query_pair_iteration_and_errors_are_bounded(self):
        from agent_qa.errors import ApiError

        consumed = 0

        def pairs():
            nonlocal consumed
            while True:
                consumed += 1
                yield ("unsupported", "x")

        with self.assertRaises(ApiError) as error:
            validate_query_params(self.PARAMETERS, pairs())
        self.assertEqual(consumed, 1001)
        self.assertEqual(len(error.exception.details), 1000)


if __name__ == "__main__":
    unittest.main()
