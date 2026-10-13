import io
import json
import unittest

from agent_qa import orders
from agent_qa.openapi import build_openapi
from agent_qa.routes import ROUTES
from agent_qa.server import Handler
from agent_qa.validation import validate


API_KEY = "qa-synthetic-key"


def invoke(method, path, payload=None, api_key=API_KEY):
    """Run one route through Handler without opening a socket."""
    encoded = b"" if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Length": str(len(encoded)), "Content-Type": "application/json"}
    if api_key is not None:
        headers["X-API-Key"] = api_key
    response = {}
    handler = object.__new__(Handler)
    handler.command = method
    handler.path = path
    handler.headers = headers
    handler.rfile = io.BytesIO(encoded)
    handler.request_id = "acceptance-test"
    handler._json = lambda status, body, extra=None: response.update(
        status=status, body=body, headers=extra or {}
    )
    handler._handle()
    return response


class Task80AcceptanceTests(unittest.TestCase):
    def test_ac1_validator_supports_schema_keywords_and_nested_paths(self):
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
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3,
                },
                "ratio": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "enabled": {"type": "boolean", "enum": [True]},
                "kind": {"type": "string", "enum": ["a", "b"]},
                "items": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "required": ["qty"],
                        "properties": {"qty": {"type": "integer"}},
                        "additionalProperties": False,
                        "minProperties": 1,
                        "maxProperties": 2,
                    },
                },
                "extra": {"type": "string"},
            },
            "additionalProperties": False,
            "minProperties": 2,
            "maxProperties": 7,
        }
        instance = {
            "name": " ",
            "count": 4,
            "ratio": -1,
            "enabled": False,
            "kind": "c",
            "items": [{"qty": 1}, {"qty": True}, {"qty": 3}, {"qty": 4}],
            "unexpected": 1,
        }

        errors = validate(schema, instance)

        fields = [error["field"] for error in errors]
        self.assertEqual(fields, sorted(fields))
        self.assertIn("count", fields)
        self.assertIn("enabled", fields)
        self.assertIn("kind", fields)
        self.assertIn("name", [error["field"] for error in errors])
        self.assertIn("ratio", [error["field"] for error in errors])
        self.assertIn("items[1].qty", [error["field"] for error in errors])
        self.assertIn({"field": "unexpected", "message": "Unknown field"}, errors)
        self.assertIn("items", fields)

        valid = {
            "name": "AB",
            "count": 2,
            "ratio": 0.5,
            "enabled": True,
            "kind": "a",
            "items": [{"qty": 1}, {"qty": 2}],
        }
        self.assertEqual(validate(schema, valid), [])
        self.assertIn(
            {"field": "name", "message": "Must not be blank"},
            validate(
                {
                    "type": "object",
                    "properties": {"name": {"type": "string", "x-nonBlank": True}},
                },
                {"name": "   "},
            ),
        )
        self.assertIn(
            "items",
            [
                error["field"]
                for error in validate(
                    {
                        "type": "object",
                        "required": ["items"],
                        "properties": {
                            "items": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "string"},
                            }
                        },
                    },
                    {"items": []},
                )
            ],
        )
        self.assertIn(
            "body",
            [
                error["field"]
                for error in validate({"type": "object", "minProperties": 1}, {})
            ],
        )
        self.assertIn(
            "body",
            [
                error["field"]
                for error in validate(
                    {"type": "object", "maxProperties": 1}, {"a": 1, "b": 2}
                )
            ],
        )
        self.assertIn(
            "name",
            [
                error["field"]
                for error in validate(
                    {
                        "type": "object",
                        "properties": {"name": {"type": "string", "maxLength": 2}},
                    },
                    {"name": "long"},
                )
            ],
        )
        self.assertEqual(
            validate(
                {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                },
                {},
            ),
            [{"field": "name", "message": "Required"}],
        )
        self.assertIn(
            "name",
            [
                error["field"]
                for error in validate(
                    {
                        "type": "object",
                        "properties": {"name": {"type": "string", "pattern": "^A"}},
                    },
                    {"name": "b"},
                )
            ],
        )

    def test_ac1_validator_checks_root_and_additional_property_schemas(self):
        self.assertEqual(
            validate({"type": "object"}, []),
            [{"field": "body", "message": "Must be a JSON object"}],
        )
        errors = validate(
            {
                "type": "object",
                "additionalProperties": {"type": "integer"},
            },
            {"a": True},
        )
        self.assertEqual(errors[0]["field"], "a")
        self.assertEqual(errors[0]["message"], "Must be an integer")

    def test_ac2_order_payloads_keep_existing_error_details(self):
        invalid_requests = (
            (
                "POST",
                "/orders",
                {},
                [
                    {"field": "customer_id", "message": "Required"},
                    {"field": "total_cents", "message": "Required"},
                ],
            ),
            (
                "POST",
                "/orders",
                {"total_cents": 1},
                [
                    {"field": "customer_id", "message": "Required"},
                ],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": "abc"},
                [
                    {"field": "total_cents", "message": "Required"},
                ],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": 1, "total_cents": True},
                [
                    {"field": "customer_id", "message": "Must be a string"},
                    {"field": "total_cents", "message": "Must be an integer"},
                ],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": "x" * 65, "total_cents": 1},
                [
                    {
                        "field": "customer_id",
                        "message": "Must contain 1 to 64 characters",
                    },
                ],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": "   ", "total_cents": 1},
                [
                    {"field": "customer_id", "message": "Must not be blank"},
                ],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": "abc", "total_cents": -1},
                [
                    {
                        "field": "total_cents",
                        "message": "Must be between 0 and 100000000",
                    },
                ],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": "abc", "total_cents": 1, "z": 1},
                [
                    {"field": "z", "message": "Unknown field"},
                ],
            ),
            (
                "PATCH",
                "/orders/1",
                {},
                [
                    {"field": "body", "message": "At least one field is required"},
                ],
            ),
            (
                "PATCH",
                "/orders/1",
                {"z": 1},
                [
                    {"field": "z", "message": "Unknown field"},
                ],
            ),
            (
                "PATCH",
                "/orders/1",
                {"total_cents": True},
                [
                    {"field": "total_cents", "message": "Must be an integer"},
                ],
            ),
            (
                "PATCH",
                "/orders/1",
                {"total_cents": 100000001},
                [
                    {
                        "field": "total_cents",
                        "message": "Must be between 0 and 100000000",
                    },
                ],
            ),
            (
                "PATCH",
                "/orders/1",
                {"status": "unknown"},
                [
                    {
                        "field": "status",
                        "message": "Must be one of: cancelled, new, paid, shipped",
                    },
                ],
            ),
            (
                "PATCH",
                "/orders/1",
                {"status": 2},
                [
                    {
                        "field": "status",
                        "message": "Must be one of: cancelled, new, paid, shipped",
                    },
                ],
            ),
            (
                "PATCH",
                "/orders/1",
                {"status": "new", "x": 1},
                [
                    {"field": "x", "message": "Unknown field"},
                ],
            ),
        )
        self.assertEqual(len(invalid_requests), 15)
        for method, path, payload, expected_details in invalid_requests:
            with self.subTest(method=method, payload=payload):
                response = invoke(method, path, payload)
                self.assertEqual(response["status"], 400)
                self.assertEqual(response["body"]["error"]["code"], "validation_error")
                self.assertEqual(response["body"]["error"]["details"], expected_details)

    def test_ac3_dispatch_authenticates_before_body_validation(self):
        response = invoke("POST", "/orders", {}, api_key=None)
        self.assertEqual(response["status"], 401)
        self.assertEqual(response["body"]["error"]["code"], "unauthorized")

    def test_ac3_dispatch_validates_before_calling_order_handler(self):
        route = next(
            route
            for route in ROUTES
            if route["method"] == "POST" and route["path"] == "/orders"
        )
        original_handler = route["handler"]
        calls = []
        route["handler"] = lambda *args: calls.append(args)
        try:
            response = invoke("POST", "/orders", {}, api_key=API_KEY)
        finally:
            route["handler"] = original_handler

        self.assertEqual(response["status"], 400)
        self.assertEqual(response["body"]["error"]["code"], "validation_error")
        self.assertEqual(
            response["body"]["error"]["message"], "Request validation failed"
        )
        self.assertEqual(calls, [])

    def test_ac3_order_store_keeps_its_validation_guard(self):
        with self.assertRaises(orders.OrderError) as error:
            orders.OrderStore().create("customer", True)
        self.assertEqual(error.exception.code, "validation_error")
        self.assertEqual(
            error.exception.details,
            [{"field": "total_cents", "message": "Must be an integer"}],
        )

    def test_ac4_query_parameter_helper_parses_types_and_errors(self):
        from agent_qa.validation import validate_query_params

        parameters = next(
            route["parameters"]
            for route in ROUTES
            if route["method"] == "GET" and route["path"] == "/orders"
        )
        result = validate_query_params(
            parameters,
            [("status", "paid"), ("limit", "2"), ("offset", "1")],
        )
        self.assertEqual(result["status"], "paid")
        self.assertEqual(result["limit"], 2)
        self.assertEqual(result["offset"], 1)
        typed = validate_query_params(
            [
                {
                    "name": "enabled",
                    "in": "query",
                    "schema": {"type": "boolean", "default": False},
                },
                {
                    "name": "label",
                    "in": "query",
                    "schema": {"type": "string"},
                },
                {
                    "name": "state",
                    "in": "query",
                    "schema": {"type": "string", "enum": ["on", "off"]},
                },
            ],
            [("enabled", "true"), ("label", "sample"), ("state", "on")],
        )
        self.assertEqual(typed["enabled"], True)
        self.assertEqual(typed["label"], "sample")
        self.assertEqual(typed["state"], "on")
        self.assertEqual(
            validate_query_params(
                [
                    {
                        "name": "enabled",
                        "in": "query",
                        "schema": {"type": "boolean", "default": False},
                    }
                ],
                [],
            )["enabled"],
            False,
        )

        for query, message in (
            ([("x", "1")], "Unsupported query parameter"),
            (
                [("limit", "2"), ("limit", "3")],
                "Parameter may appear once",
            ),
            ([("limit", "x")], "Must be an integer from 1 to 100"),
        ):
            with self.subTest(query=query), self.assertRaises(Exception) as error:
                validate_query_params(parameters, query)
            self.assertEqual(str(error.exception), message)

    def test_ac4_schema_registry_routes_and_validation_api(self):
        from agent_qa.schemas import SCHEMAS

        self.assertIn("CreateOrder", SCHEMAS)
        self.assertIn("UpdateOrder", SCHEMAS)
        self.assertIn("Order", SCHEMAS)
        self.assertIn("OrderList", SCHEMAS)
        self.assertIn("Error", SCHEMAS)
        create_route = next(
            route
            for route in ROUTES
            if route["method"] == "POST" and route["path"] == "/orders"
        )
        patch_route = next(
            route
            for route in ROUTES
            if route["method"] == "PATCH" and route["path"] == "/orders/{id}"
        )
        self.assertIs(create_route["request_schema"], SCHEMAS["CreateOrder"])
        self.assertIs(patch_route["request_schema"], SCHEMAS["UpdateOrder"])

        listed = invoke("GET", "/schemas", api_key=None)
        self.assertEqual(listed["body"]["items"], sorted(SCHEMAS))
        fetched = invoke("GET", "/schemas/CreateOrder", api_key=None)
        self.assertEqual(fetched["body"], SCHEMAS["CreateOrder"])
        missing = invoke("GET", "/schemas/missing", api_key=None)
        self.assertEqual(missing["status"], 404)
        self.assertEqual(missing["body"]["error"]["code"], "schema_not_found")
        checked = invoke("POST", "/schemas/CreateOrder/validate", {}, api_key=None)
        self.assertEqual(checked["status"], 200)
        self.assertEqual(checked["body"]["valid"], False)
        self.assertEqual(checked["body"]["errors"][0]["field"], "customer_id")
        self.assertEqual(checked["body"]["errors"][0]["message"], "Required")

    def test_ac5_openapi_contains_named_schemas_and_inline_order_schemas(self):
        from agent_qa.schemas import SCHEMAS

        spec = build_openapi(ROUTES, "acceptance-sha")
        components = spec["components"]["schemas"]
        for name in ("CreateOrder", "UpdateOrder", "Order", "OrderList", "Error"):
            self.assertIn(name, components)
        create_inline = spec["paths"]["/orders"]["post"]["requestBody"]
        create_inline = create_inline["content"]["application/json"]["schema"]
        patch_inline = spec["paths"]["/orders/{id}"]["patch"]["requestBody"]
        patch_inline = patch_inline["content"]["application/json"]["schema"]
        self.assertEqual(create_inline, SCHEMAS["CreateOrder"])
        self.assertEqual(patch_inline, SCHEMAS["UpdateOrder"])
        for method, path in (
            ("get", "/schemas"),
            ("get", "/schemas/{name}"),
            ("post", "/schemas/{name}/validate"),
        ):
            self.assertIn(path, spec["paths"])
            self.assertIn(method, spec["paths"][path])
            self.assertNotIn("security", spec["paths"][path][method])


if __name__ == "__main__":
    unittest.main()
