"""Tests for the shared JSON-schema subset and query parameter parser."""

import math
import unittest

from agent_qa.errors import ApiError
from agent_qa.validation import validate, validate_query_params


class ValidationTests(unittest.TestCase):
    def test_order_routes_share_registry_request_schemas(self):
        from agent_qa.routes import ROUTES
        from agent_qa.schemas import SCHEMAS

        for method, path, name in (
            ("POST", "/orders", "CreateOrder"),
            ("PATCH", "/orders/{id}", "UpdateOrder"),
        ):
            route = next(
                route
                for route in ROUTES
                if route["method"] == method and route["path"] == path
            )
            self.assertIs(route["request_schema"], SCHEMAS[name])

    def test_query_input_sizes_are_bounded(self):
        for query, message in (
            ([("x", "1")] * 129, "Too many query parameters"),
            ([("x" * 129, "1")], "Parameter name is too long"),
        ):
            with self.subTest(message=message), self.assertRaises(ApiError) as error:
                validate_query_params([], query)
            self.assertEqual(error.exception.status, 400)
            self.assertEqual(str(error.exception), message)

    def test_supported_schema_keywords_and_nested_paths(self):
        schema = {
            "type": "object",
            "required": ["items", "name"],
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 4,
                    "pattern": "^[A-Z]+$",
                    "x-nonBlank": True,
                },
                "count": {"type": "integer", "minimum": 1, "maximum": 3},
                "ratio": {"type": "number", "minimum": 0, "maximum": 1},
                "enabled": {"type": "boolean", "enum": [True]},
                "items": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "required": ["qty"],
                        "properties": {"qty": {"type": "integer"}},
                        "additionalProperties": {"type": "string"},
                        "minProperties": 1,
                        "maxProperties": 2,
                    },
                },
            },
            "additionalProperties": False,
            "minProperties": 2,
            "maxProperties": 7,
        }
        errors = validate(
            schema,
            {
                "name": "  ",
                "count": True,
                "ratio": -1,
                "enabled": False,
                "items": [{"qty": 1}, {"qty": True}],
                "unexpected": 1,
            },
        )
        fields = [error["field"] for error in errors]
        self.assertEqual(fields, sorted(fields))
        self.assertIn({"field": "count", "message": "Must be an integer"}, errors)
        self.assertIn(
            {"field": "items[1].qty", "message": "Must be an integer"}, errors
        )
        self.assertIn({"field": "unexpected", "message": "Unknown field"}, errors)
        self.assertIn({"field": "name", "message": "Must not be blank"}, errors)

    def test_required_unknown_and_exact_order_messages(self):
        from agent_qa.orders import OrderError, validate_create, validate_patch

        cases = (
            (
                validate_create,
                {},
                [
                    {"field": "customer_id", "message": "Required"},
                    {"field": "total_cents", "message": "Required"},
                ],
            ),
            (
                validate_create,
                {"customer_id": 3, "total_cents": True},
                [
                    {"field": "customer_id", "message": "Must be a string"},
                    {"field": "total_cents", "message": "Must be an integer"},
                ],
            ),
            (
                validate_create,
                {"customer_id": 3, "total_cents": True, "z": 1},
                [
                    {"field": "z", "message": "Unknown field"},
                    {"field": "customer_id", "message": "Must be a string"},
                    {"field": "total_cents", "message": "Must be an integer"},
                ],
            ),
            (
                validate_create,
                {"customer_id": "x" * 65, "total_cents": 1},
                [
                    {
                        "field": "customer_id",
                        "message": "Must contain 1 to 64 characters",
                    },
                ],
            ),
            (
                validate_create,
                {"customer_id": " ", "total_cents": 1},
                [
                    {"field": "customer_id", "message": "Must not be blank"},
                ],
            ),
            (
                validate_create,
                {"customer_id": "abc", "total_cents": -1},
                [
                    {
                        "field": "total_cents",
                        "message": "Must be between 0 and 100000000",
                    },
                ],
            ),
            (
                validate_patch,
                {},
                [
                    {"field": "body", "message": "At least one field is required"},
                ],
            ),
            (
                validate_patch,
                {"status": 2},
                [
                    {
                        "field": "status",
                        "message": "Must be one of: cancelled, new, paid, shipped",
                    },
                ],
            ),
        )
        for validator, payload, details in cases:
            with self.subTest(payload=payload), self.assertRaises(OrderError) as error:
                validator(payload)
            self.assertEqual(error.exception.details, details)

    def test_boolean_is_not_integer_and_root_object_message(self):
        self.assertEqual(
            validate({"type": "integer"}, True),
            [{"field": "body", "message": "Must be an integer"}],
        )
        self.assertEqual(
            validate({"type": "object"}, []),
            [{"field": "body", "message": "Must be a JSON object"}],
        )
        self.assertEqual(
            validate(
                {
                    "type": "object",
                    "properties": {
                        "body": {
                            "type": "object",
                            "properties": {"qty": {"type": "integer"}},
                        }
                    },
                },
                {"body": {"qty": True}},
            ),
            [{"field": "body.qty", "message": "Must be an integer"}],
        )

    def test_non_finite_floats_are_not_json_numbers(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                self.assertEqual(
                    validate({"type": "number"}, value),
                    [{"field": "body", "message": "Must be a number"}],
                )

    def test_primitive_types_enum_and_string_constraints(self):
        schema = {
            "type": "object",
            "properties": {
                "object": {"type": "object"},
                "array": {"type": "array"},
                "string": {"type": "string", "minLength": 2, "maxLength": 4},
                "integer": {"type": "integer"},
                "number": {"type": "number", "minimum": 1, "maximum": 2},
                "boolean": {"type": "boolean"},
                "status": {"type": "string", "enum": ["new", "paid"]},
                "numeric_enum": {"type": "integer", "enum": [1]},
                "pattern": {"type": "string", "pattern": "^[A-Z]+$"},
            },
        }
        errors = validate(
            schema,
            {
                "object": [],
                "array": {},
                "string": "a",
                "integer": False,
                "number": 3,
                "boolean": "yes",
                "status": 3,
                "numeric_enum": True,
                "pattern": "lower",
            },
        )
        by_field = {error["field"]: error["message"] for error in errors}
        self.assertEqual(by_field["object"], "Must be a JSON object")
        self.assertEqual(by_field["array"], "Must be an array")
        self.assertEqual(by_field["integer"], "Must be an integer")
        self.assertEqual(by_field["number"], "Must be between 1 and 2")
        self.assertEqual(by_field["boolean"], "Must be a boolean")
        self.assertEqual(by_field["status"], "Must be one of: new, paid")
        self.assertEqual(by_field["numeric_enum"], "Must be an integer")
        self.assertEqual(by_field["pattern"], "Must match the required pattern")
        self.assertEqual(by_field["string"], "Must contain 2 to 4 characters")
        self.assertEqual(
            validate({"type": "string", "minLength": 1, "maxLength": 3}, "long"),
            [{"field": "body", "message": "Must contain 1 to 3 characters"}],
        )
        self.assertEqual(validate({"type": "number", "enum": [1]}, 1.0), [])

    def test_nested_enum_uses_recursive_json_equality(self):
        enum_value = {
            "label": "ok",
            "nested": {"items": [1, {"flag": True}]},
        }
        schema = {"enum": [enum_value]}

        self.assertEqual(
            validate(
                schema,
                {"nested": {"items": [1.0, {"flag": True}]}, "label": "ok"},
            ),
            [],
        )

        invalid_values = (
            {"label": "ok", "nested": {"items": [True, {"flag": True}]}},
            {"label": "ok", "nested": {"items": [1, {"flag": 1}]}},
            {"label": "ok", "nested": {"items": [1, {"flag": True}, 2]}},
            {"label": "ok", "nested": {"items": [{"flag": True}, 1]}},
            {"label": "ok", "nested": {"items": [1, {"other": True}]}},
        )
        for value in invalid_values:
            with self.subTest(value=value):
                errors = validate(schema, value)
                self.assertEqual([error["field"] for error in errors], ["body"])

        reverse_schema = {"enum": [{"nested": [{"qty": True}]}]}
        errors = validate(reverse_schema, {"nested": [{"qty": 1}]})
        self.assertEqual([error["field"] for error in errors], ["body"])

    def test_container_size_limits_and_typed_additional_properties(self):
        schema = {
            "type": "object",
            "minProperties": 2,
            "maxProperties": 2,
            "properties": {
                "items": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 2,
                    "items": {"type": "integer"},
                }
            },
            "additionalProperties": {"type": "string"},
        }
        errors = validate(schema, {"items": [1], "extra": 3, "another": "x"})
        fields = {error["field"] for error in errors}
        self.assertIn("body", fields)
        self.assertIn("items", fields)
        self.assertIn("extra", fields)
        self.assertEqual(
            validate(
                {"type": "array", "minItems": 1, "items": {"type": "string"}},
                [],
            ),
            [{"field": "body", "message": "Must contain at least 1 items"}],
        )
        self.assertEqual(
            validate({"type": "array", "maxItems": 2}, [1, 2, 3]),
            [{"field": "body", "message": "Must contain at most 2 items"}],
        )
        self.assertEqual(
            validate({"type": "object", "minProperties": 2}, {"one": 1}),
            [{"field": "body", "message": "Must have at least 2 properties"}],
        )

    def test_validation_complexity_is_bounded(self):
        errors = validate(
            {"type": "array", "items": {"type": "integer"}},
            [True] * 10_001,
        )
        self.assertEqual(
            errors,
            [
                {
                    "field": "body",
                    "message": "Maximum validation complexity exceeded",
                }
            ],
        )

    def test_query_parameters_are_typed_defaulted_and_bounded(self):
        parameters = [
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
                "name": "enabled",
                "in": "query",
                "schema": {"type": "boolean", "default": False},
            },
            {
                "name": "state",
                "in": "query",
                "schema": {"type": "string", "enum": ["on", "off"]},
            },
        ]
        self.assertEqual(
            validate_query_params(
                parameters,
                [("limit", "2"), ("enabled", "true"), ("state", "on")],
            ),
            {"limit": 2, "enabled": True, "state": "on"},
        )
        self.assertEqual(
            validate_query_params(parameters, []),
            {"limit": 20, "enabled": False},
        )
        for pairs, message in (
            ([("x", "1")], "Unsupported query parameter"),
            ([("limit", "2"), ("limit", "3")], "Parameter may appear once"),
            (
                [("limit", "100000000000000000000000000000000000000000000000000")],
                "Must be an integer from 1 to 100",
            ),
        ):
            with self.subTest(pairs=pairs), self.assertRaises(ApiError) as error:
                validate_query_params(parameters, pairs)
            self.assertEqual(error.exception.details[0]["message"], message)


if __name__ == "__main__":
    unittest.main()
