"""Drift tests for the route table and generated OpenAPI document."""

import copy
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_qa import orders, products, schemas
from agent_qa.openapi import build_openapi
from agent_qa.pagination import MAX_CURSOR_LENGTH
from agent_qa.routes import ROUTES
from agent_qa.schemas import SCHEMAS


ROOT = Path(__file__).resolve().parents[1]
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


class OpenApiSchemaUnitTests(unittest.TestCase):
    def test_bulk_route_metadata_matches_openapi(self):
        spec = build_openapi(ROUTES, "bulk-drift")
        route_pairs = {(route["method"].lower(), route["path"]) for route in ROUTES}
        spec_pairs = {
            (method, path)
            for path, path_item in spec["paths"].items()
            for method in path_item
        }
        self.assertEqual(spec_pairs, route_pairs)
        for route in ROUTES:
            operation = spec["paths"][route["path"]][route["method"].lower()]
            self.assertEqual(operation["x-required-role"], route["role"])
            self.assertEqual(route["auth_required"], route["role"] is not None)
            self.assertIn("403", operation["responses"])
            if route["path"] not in ("/orders/bulk", "/products/bulk"):
                continue
            self.assertEqual(set(operation["responses"]), set(route["responses"]))
            self.assertEqual(operation["x-max-body-bytes"], route["max_body_bytes"])
            self.assertEqual(
                operation["requestBody"]["content"]["application/json"]["schema"],
                route["request_schema"],
            )

    def test_key_management_routes_are_documented_with_roles_and_forbidden(self):
        spec = build_openapi(ROUTES, "keys-test")
        expected = {
            ("/whoami", "get"): "read",
            ("/admin/keys", "get"): "admin",
            ("/admin/keys", "post"): "admin",
            ("/admin/keys/{key_id}/rotate", "post"): "admin",
            ("/admin/keys/{key_id}", "delete"): "admin",
        }
        for (path, method), role in expected.items():
            operation = spec["paths"][path][method]
            self.assertEqual(operation["x-required-role"], role)
            self.assertIn("403", operation["responses"])
            self.assertIn(
                "#/components/schemas/Error",
                json.dumps(operation["responses"]["403"]),
            )
            self.assertTrue(operation.get("security"))
        self.assertIn("409", spec["paths"]["/admin/keys"]["post"]["responses"])
        self.assertIn(
            "key_not_found",
            json.dumps(spec["paths"]["/admin/keys/{key_id}"]["delete"]),
        )
        list_schema = spec["paths"]["/admin/keys"]["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]
        self.assertEqual(
            list_schema["properties"]["items"]["items"]["required"],
            [
                "key_id",
                "role",
                "label",
                "created_at",
                "last_used_at",
                "fingerprint",
                "status",
            ],
        )

    def test_bulk_routes_document_body_limits_and_result_statuses(self):
        spec = build_openapi(ROUTES, "bulk-test")
        for path in ("/orders/bulk", "/products/bulk"):
            with self.subTest(path=path):
                operation = spec["paths"][path]["post"]
                self.assertEqual(operation["x-max-body-bytes"], 65536)
                request = operation["requestBody"]["content"]["application/json"]
                self.assertEqual(
                    request["schema"]["properties"]["items"]["minItems"], 1
                )
                self.assertEqual(
                    request["schema"]["properties"]["items"]["maxItems"], 50
                )
                self.assertIn("atomic", request["schema"]["properties"])
                for status in ("201", "207", "422"):
                    self.assertIn(status, operation["responses"])
                self.assertIn("Idempotency-Key", json.dumps(operation["parameters"]))
                self.assertIn("BulkCreateResponse", json.dumps(operation["responses"]))
        self.assertEqual(
            spec["paths"]["/orders/bulk"]["post"]["responses"]["422"]["headers"].keys(),
            {"Idempotency-Key", "Idempotent-Replay", "X-Request-Id"},
        )
        self.assertEqual(spec["paths"]["/orders"]["post"]["x-max-body-bytes"], 4096)

        create_order_route = next(
            route
            for route in ROUTES
            if route["path"] == "/orders" and route["method"] == "POST"
        )
        custom_route = copy.deepcopy(create_order_route)
        custom_route["max_body_bytes"] = 8192
        custom_spec = build_openapi([custom_route], "bulk-test")
        self.assertEqual(
            custom_spec["paths"]["/orders"]["post"]["x-max-body-bytes"], 8192
        )

    def test_cursor_pagination_parameters_responses_and_errors_are_documented(self):
        spec = build_openapi(ROUTES, "cursor-pagination-test")
        for path in ("/orders", "/products"):
            with self.subTest(path=path):
                operation = spec["paths"][path]["get"]
                parameters = {item["name"]: item for item in operation["parameters"]}
                self.assertEqual(
                    parameters["pagination"]["schema"]["enum"],
                    ["offset", "cursor"],
                )
                self.assertEqual(
                    parameters["pagination"]["schema"]["default"], "offset"
                )
                self.assertEqual(parameters["cursor"]["in"], "query")
                self.assertEqual(
                    parameters["cursor"]["schema"]["maxLength"],
                    MAX_CURSOR_LENGTH,
                )
                self.assertGreaterEqual(
                    parameters["pagination"]["description"].find("offset"), 0
                )
                self.assertIn("Link", operation["responses"]["200"]["headers"])
                self.assertIn(
                    "invalid_cursor",
                    operation["responses"]["400"]["description"],
                )
                self.assertIn(
                    "cursor_mismatch",
                    operation["responses"]["400"]["description"],
                )
                response_schema = operation["responses"]["200"]["content"][
                    "application/json"
                ]["schema"]
                self.assertNotIn("offset", response_schema["required"])
                self.assertTrue(
                    response_schema["properties"]["next_cursor"]["nullable"]
                )
        order_parameters = {
            item["name"]: item["schema"]
            for item in spec["paths"]["/orders"]["get"]["parameters"]
        }
        self.assertEqual(order_parameters["sort"]["enum"], ["id", "-id"])

    def test_conditional_request_headers_and_responses_are_documented(self):
        spec = build_openapi(ROUTES, "conditional-test")
        cases = (
            ("/orders", "get", "If-None-Match", {"200", "304", "400"}),
            ("/orders/{id}", "get", "If-None-Match", {"200", "304", "400", "404"}),
            ("/orders/{id}", "patch", "If-Match", {"200", "400", "412", "428"}),
            ("/products", "get", "If-None-Match", {"200", "304", "400"}),
            ("/products/{id}", "delete", "If-Match", {"204", "400", "412", "428"}),
            (
                "/products/{id}/adjust-stock",
                "post",
                "If-Match",
                {"200", "400", "412", "428"},
            ),
        )
        for path, method, header, response_codes in cases:
            with self.subTest(path=path, method=method):
                operation = spec["paths"][path][method]
                parameters = operation["parameters"]
                self.assertIn(
                    header,
                    [parameter["name"] for parameter in parameters],
                )
                self.assertTrue(response_codes.issubset(operation["responses"]))
                if "412" in response_codes:
                    self.assertIn(
                        "#/components/schemas/Error",
                        json.dumps(operation["responses"]["412"]),
                    )
        self.assertNotIn("content", spec["paths"]["/orders"]["get"]["responses"]["304"])
        self.assertIn(
            "ETag",
            spec["paths"]["/products"]["get"]["responses"]["304"]["headers"],
        )

    def test_order_schemas_export_item_and_exactly_one_limits(self):
        spec = build_openapi(ROUTES, "order-schema-test")
        self.assertEqual(spec["components"]["schemas"], SCHEMAS)
        create = spec["paths"]["/orders"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        self.assertEqual(create["x-exactlyOne"], ["items", "total_cents"])
        self.assertEqual(
            create["oneOf"],
            [{"required": ["items"]}, {"required": ["total_cents"]}],
        )
        self.assertEqual(create["properties"]["items"]["minItems"], 1)
        self.assertEqual(
            create["properties"]["items"]["maxItems"], schemas.MAX_ORDER_ITEMS
        )
        self.assertEqual(
            create["properties"]["items"]["items"]["properties"]["quantity"]["maximum"],
            schemas.MAX_ORDER_QUANTITY,
        )
        self.assertIn("items", SCHEMAS["Order"]["required"])
        self.assertEqual(
            set(SCHEMAS["Order"]["properties"]["items"]["items"]["required"]),
            {
                "product_id",
                "sku",
                "name",
                "quantity",
                "unit_price_cents",
                "line_total_cents",
            },
        )

    def test_idempotent_post_documents_key_errors_and_replay_headers(self):
        spec = build_openapi(ROUTES, "idempotency-test")
        create = spec["paths"]["/orders"]["post"]

        key_parameter = next(
            parameter
            for parameter in create["parameters"]
            if parameter["name"] == "Idempotency-Key"
        )
        self.assertEqual(key_parameter["in"], "header")
        self.assertFalse(key_parameter["required"])
        self.assertEqual(
            key_parameter["schema"],
            {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "pattern": "^[A-Za-z0-9._:-]+$",
            },
        )
        self.assertIn("idempotency_key_reused", json.dumps(create["responses"]["422"]))
        self.assertIn("idempotency_in_progress", json.dumps(create["responses"]["409"]))
        self.assertIn("invalid_idempotency_key", json.dumps(create["responses"]["400"]))
        success_headers = create["responses"]["201"]["headers"]
        self.assertEqual(
            set(success_headers),
            {"ETag", "Idempotency-Key", "Idempotent-Replay", "X-Request-Id"},
        )

        future_route = copy.deepcopy(ROUTES[0])
        future_route.update(
            {
                "method": "POST",
                "path": "/orders/bulk",
                "idempotent": True,
                "operation_id": "createOrdersBulk",
                "summary": "Create orders in bulk",
                "responses": ["201"],
            }
        )
        future = build_openapi([future_route], "idempotency-test")
        future_operation = future["paths"]["/orders/bulk"]["post"]
        future_parameters = future_operation["parameters"]
        future_parameter_names = [parameter["name"] for parameter in future_parameters]
        self.assertIn("Idempotency-Key", future_parameter_names)
        self.assertIn("409", future_operation["responses"])
        self.assertIn("422", future_operation["responses"])
        self.assertIn("400", future_operation["responses"])

        product = spec["paths"]["/products"]["post"]
        product_parameter_names = [
            parameter["name"] for parameter in product["parameters"]
        ]
        self.assertIn("Idempotency-Key", product_parameter_names)

        non_post = copy.deepcopy(future_route)
        non_post.update({"method": "GET", "path": "/orders/bulk", "responses": ["200"]})
        non_post_spec = build_openapi([non_post], "idempotency-test")
        non_post_operation = non_post_spec["paths"]["/orders/bulk"]["get"]
        self.assertNotIn("parameters", non_post_operation)
        self.assertNotIn("409", non_post_operation["responses"])


class OpenApiDriftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        env = {
            "APP_PORT": str(cls.port),
            "AGENT_QA_GIT_SHA": "openapi-test-sha",
        }
        cls.process = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.base = f"http://127.0.0.1:{cls.port}"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with urlopen(cls.base + "/ready", timeout=0.2):
                    return
            except Exception:
                time.sleep(0.05)
        cls.process.terminate()
        cls.process.wait(timeout=3)
        raise RuntimeError("service did not start")

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=3)

    @classmethod
    def request(cls, method, path):
        request = Request(cls.base + path, method=method)
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    @classmethod
    def live_spec(cls):
        status, headers, body = cls.request("GET", "/openapi.json")
        if status != 200:
            raise AssertionError(f"GET /openapi.json returned {status}")
        if "application/json" not in headers.get("Content-Type", ""):
            raise AssertionError("GET /openapi.json did not return JSON")
        return json.loads(body)

    @staticmethod
    def concrete_path(template):
        return (
            template.replace("{id}", "1")
            .replace("{name}", "CreateOrder")
            .replace("{key_id}", "key_1")
        )

    def test_route_table_and_spec_have_the_same_method_path_pairs(self):
        spec = self.live_spec()
        route_pairs = {(route["method"].lower(), route["path"]) for route in ROUTES}
        spec_pairs = {
            (method, path)
            for path, path_item in spec["paths"].items()
            for method in path_item
            if method in {"get", "post", "put", "patch", "delete", "head", "options"}
        }
        self.assertEqual(spec_pairs, route_pairs)

    def test_build_openapi_uses_routes_argument(self):
        synthetic = copy.deepcopy(ROUTES[0])
        synthetic.update(
            {
                "method": "GET",
                "path": "/synthetic-openapi-drift",
                "operation_id": "getSyntheticOpenapiDrift",
                "summary": "Synthetic route for drift coverage",
            }
        )
        spec = build_openapi((*ROUTES, synthetic), "synthetic-sha")
        self.assertIn("/synthetic-openapi-drift", spec["paths"])
        self.assertIn("get", spec["paths"]["/synthetic-openapi-drift"])

    def test_live_405_allow_headers_match_spec_methods(self):
        spec = self.live_spec()
        for template, path_item in spec["paths"].items():
            documented = sorted(
                method.upper()
                for method in path_item
                if method
                in {"get", "post", "put", "patch", "delete", "head", "options"}
            )
            unsupported = next(
                method for method in HTTP_METHODS if method not in documented
            )
            with self.subTest(path=template):
                status, headers, _ = self.request(
                    unsupported, self.concrete_path(template)
                )
                self.assertEqual(status, 405)
                self.assertEqual(headers.get("Allow"), ", ".join(documented))

    def test_live_authentication_matches_operation_security(self):
        spec = self.live_spec()
        auth_by_operation = {
            (route["path"], route["method"].lower()): route["auth_required"]
            for route in ROUTES
        }
        for template, path_item in spec["paths"].items():
            for method, operation in path_item.items():
                if method not in {
                    "get",
                    "post",
                    "put",
                    "patch",
                    "delete",
                    "head",
                    "options",
                }:
                    continue
                with self.subTest(path=template, method=method):
                    requires_auth = bool(operation.get("security"))
                    self.assertEqual(
                        requires_auth, auth_by_operation[(template, method)]
                    )
                    status, _, _ = self.request(
                        method.upper(), self.concrete_path(template)
                    )
                    if requires_auth:
                        self.assertEqual(status, 401)
                    else:
                        self.assertNotEqual(status, 401)

    def test_order_enums_and_limits_come_from_orders_constants(self):
        spec = self.live_spec()
        query_parameters = spec["paths"]["/orders"]["get"]["parameters"]
        parameters = {
            parameter["name"]: parameter["schema"] for parameter in query_parameters
        }
        status_values = parameters["status"]["enum"]
        self.assertEqual(status_values, list(orders.STATUSES))
        self.assertEqual(parameters["limit"]["minimum"], orders.MIN_LIMIT)
        self.assertEqual(parameters["limit"]["maximum"], orders.MAX_LIMIT)
        self.assertEqual(parameters["limit"]["default"], orders.DEFAULT_LIMIT)
        self.assertEqual(parameters["offset"]["minimum"], orders.MIN_OFFSET)
        self.assertEqual(parameters["offset"]["default"], orders.DEFAULT_OFFSET)
        self.assertEqual(parameters["offset"]["maximum"], (1 << 63) - 1)
        self.assertEqual(parameters["sort"]["enum"], ["id", "-id"])
        self.assertEqual(parameters["pagination"]["default"], "offset")

        create_schema = spec["paths"]["/orders"]["post"]["requestBody"]
        create_schema = create_schema["content"]["application/json"]["schema"]
        customer_schema = create_schema["properties"]["customer_id"]
        total_schema = create_schema["properties"]["total_cents"]
        self.assertEqual(customer_schema["minLength"], orders.MIN_CUSTOMER_ID_LENGTH)
        self.assertEqual(customer_schema["maxLength"], orders.MAX_CUSTOMER_ID_LENGTH)
        self.assertEqual(total_schema["minimum"], orders.MIN_TOTAL_CENTS)
        self.assertEqual(total_schema["maximum"], orders.MAX_TOTAL_CENTS)
        self.assertEqual(create_schema["x-exactlyOne"], ["items", "total_cents"])
        items_schema = create_schema["properties"]["items"]
        self.assertEqual(items_schema["minItems"], 1)
        self.assertEqual(items_schema["maxItems"], schemas.MAX_ORDER_ITEMS)
        item_schema = items_schema["items"]
        self.assertEqual(item_schema["properties"]["product_id"]["minimum"], 1)
        self.assertEqual(item_schema["properties"]["quantity"]["minimum"], 1)
        self.assertEqual(
            item_schema["properties"]["quantity"]["maximum"],
            schemas.MAX_ORDER_QUANTITY,
        )
        order_item_schema = SCHEMAS["Order"]["properties"]["items"]["items"]
        self.assertEqual(
            set(order_item_schema["properties"]),
            {
                "product_id",
                "sku",
                "name",
                "quantity",
                "unit_price_cents",
                "line_total_cents",
            },
        )
        self.assertIn("items", SCHEMAS["Order"]["required"])

        patch_schema = spec["paths"]["/orders/{id}"]["patch"]["requestBody"]
        patch_schema = patch_schema["content"]["application/json"]["schema"]
        patch_total = patch_schema["properties"]["total_cents"]
        patch_status = patch_schema["properties"]["status"]["enum"]
        self.assertEqual(patch_total["minimum"], orders.MIN_TOTAL_CENTS)
        self.assertEqual(patch_total["maximum"], orders.MAX_TOTAL_CENTS)
        self.assertEqual(patch_status, list(orders.STATUSES))

    def test_named_schemas_are_documented_and_order_bodies_stay_inline(self):
        spec = self.live_spec()
        self.assertEqual(spec["components"]["schemas"], SCHEMAS)
        self.assertEqual(
            spec["paths"]["/orders"]["post"]["requestBody"]["content"][
                "application/json"
            ]["schema"],
            SCHEMAS["CreateOrder"],
        )
        self.assertEqual(
            spec["paths"]["/orders/{id}"]["patch"]["requestBody"]["content"][
                "application/json"
            ]["schema"],
            SCHEMAS["UpdateOrder"],
        )
        self.assertIn("/schemas", spec["paths"])
        self.assertIn("/schemas/{name}", spec["paths"])
        self.assertIn("/schemas/{name}/validate", spec["paths"])
        self.assertIn("requestBody", spec["paths"]["/schemas/{name}/validate"]["post"])

    def test_product_routes_schemas_and_query_limits_are_documented(self):
        spec = self.live_spec()
        paths = spec["paths"]
        self.assertTrue(
            {
                ("post", "/products"),
                ("get", "/products"),
                ("get", "/products/{id}"),
                ("patch", "/products/{id}"),
                ("delete", "/products/{id}"),
                ("post", "/products/{id}/adjust-stock"),
                ("get", "/categories"),
            }.issubset(
                {(method, path) for path, item in paths.items() for method in item}
            )
        )
        self.assertEqual(spec["components"]["schemas"], SCHEMAS)

        create_body = paths["/products"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        patch_body = paths["/products/{id}"]["patch"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        adjust_body = paths["/products/{id}/adjust-stock"]["post"]["requestBody"][
            "content"
        ]["application/json"]["schema"]
        self.assertEqual(create_body, SCHEMAS["CreateProduct"])
        self.assertEqual(patch_body, SCHEMAS["UpdateProduct"])
        self.assertEqual(adjust_body, SCHEMAS["AdjustStock"])

        product_properties = create_body["properties"]
        self.assertEqual(
            product_properties["price_cents"]["minimum"], schemas.MIN_PRICE_CENTS
        )
        self.assertEqual(
            product_properties["price_cents"]["maximum"], schemas.MAX_PRICE_CENTS
        )
        self.assertEqual(product_properties["stock"]["minimum"], schemas.MIN_STOCK)
        self.assertEqual(product_properties["stock"]["maximum"], schemas.MAX_STOCK)
        self.assertEqual(product_properties["sku"]["minLength"], 2)
        self.assertEqual(product_properties["sku"]["maxLength"], schemas.MAX_SKU_LENGTH)
        self.assertEqual(
            product_properties["sku"]["pattern"], "^[A-Z0-9][A-Z0-9-]{1,31}$"
        )
        self.assertTrue(product_properties["sku"]["x-fullMatch"])
        self.assertEqual(
            product_properties["name"]["minLength"], schemas.MIN_PRODUCT_NAME_LENGTH
        )
        self.assertEqual(
            product_properties["name"]["maxLength"], schemas.MAX_PRODUCT_NAME_LENGTH
        )
        self.assertTrue(product_properties["name"]["x-nonBlank"])
        self.assertEqual(
            product_properties["category"]["maxLength"], schemas.MAX_CATEGORY_LENGTH
        )
        self.assertEqual(
            product_properties["category"]["pattern"],
            "^[a-z0-9][a-z0-9-]{0,31}$",
        )
        self.assertTrue(product_properties["category"]["x-fullMatch"])
        tags_schema = product_properties["tags"]
        self.assertEqual(tags_schema["maxItems"], schemas.MAX_PRODUCT_TAGS)
        self.assertTrue(tags_schema["uniqueItems"])
        self.assertEqual(
            tags_schema["items"]["maxLength"], schemas.MAX_PRODUCT_TAG_LENGTH
        )
        self.assertTrue(tags_schema["items"]["x-fullMatch"])
        self.assertEqual(product_properties["active"]["type"], "boolean")
        self.assertEqual(
            SCHEMAS["ProductList"]["properties"]["total"]["maximum"],
            products.MAX_PRODUCTS,
        )
        self.assertEqual(
            SCHEMAS["CategoryList"]["properties"]["total"]["maximum"],
            products.MAX_PRODUCTS,
        )
        self.assertEqual(schemas.MAX_PRODUCTS, products.MAX_PRODUCTS)

        parameters = {
            parameter["name"]: parameter["schema"]
            for parameter in paths["/products"]["get"]["parameters"]
        }
        self.assertEqual(parameters["sort"]["enum"], list(schemas.PRODUCT_SORTS))
        self.assertEqual(parameters["q"]["maxLength"], schemas.MAX_PRODUCT_QUERY_LENGTH)
        self.assertEqual(parameters["limit"]["minimum"], orders.MIN_LIMIT)
        self.assertEqual(parameters["limit"]["maximum"], orders.MAX_LIMIT)
        self.assertEqual(parameters["limit"]["default"], orders.DEFAULT_LIMIT)
        self.assertEqual(parameters["offset"]["minimum"], orders.MIN_OFFSET)
        self.assertEqual(parameters["offset"]["default"], orders.DEFAULT_OFFSET)
        self.assertEqual(parameters["offset"]["maximum"], (1 << 63) - 1)
        self.assertEqual(parameters["pagination"]["default"], "offset")
        delta_schema = SCHEMAS["AdjustStock"]["properties"]["delta"]
        self.assertEqual(delta_schema["minimum"], -schemas.MAX_STOCK)
        self.assertEqual(delta_schema["maximum"], schemas.MAX_STOCK)
        self.assertTrue(delta_schema["x-nonZero"])

        for path, method in (
            ("/products", "post"),
            ("/products/{id}", "patch"),
            ("/products/{id}", "delete"),
            ("/products/{id}/adjust-stock", "post"),
        ):
            self.assertTrue(paths[path][method].get("security"))
        self.assertNotIn(
            "content", paths["/products/{id}"]["delete"]["responses"]["204"]
        )

    def test_response_content_documents_json_and_preserves_bodyless_responses(self):
        spec = self.live_spec()

        health_response = spec["paths"]["/health"]["get"]["responses"]["200"]
        health_schema = health_response["content"]["application/json"]["schema"]
        self.assertEqual(health_schema["required"], ["status"])
        self.assertEqual(
            health_schema["properties"],
            {"status": {"type": "string", "enum": ["ok"]}},
        )
        self.assertFalse(health_schema["additionalProperties"])

        about_response = spec["paths"]["/about"]["get"]["responses"]["200"]
        about_schema = about_response["content"]["application/json"]["schema"]
        self.assertEqual(
            about_schema["required"], ["service", "git_sha", "environment"]
        )
        self.assertEqual(
            about_schema["properties"],
            {
                "service": {"type": "string", "enum": ["agent-qa"]},
                "git_sha": {"type": "string"},
                "environment": {"type": "string", "enum": ["qa"]},
            },
        )
        self.assertFalse(about_schema["additionalProperties"])

        ping_response = spec["paths"]["/ping"]["get"]["responses"]["200"]
        ping_schema = ping_response["content"]["application/json"]["schema"]
        self.assertEqual(ping_schema["required"], ["pong"])
        self.assertEqual(
            ping_schema["properties"]["pong"],
            {"type": "boolean", "enum": [True]},
        )

        orders_response = spec["paths"]["/orders"]["get"]["responses"]["200"]
        json_content = orders_response["content"]["application/json"]
        schema = json_content["schema"]
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["properties"]["items"]["type"], "array")
        self.assertEqual(schema["properties"]["items"]["items"]["type"], "object")

        delete_response = spec["paths"]["/orders/{id}"]["delete"]["responses"]["204"]
        self.assertNotIn("content", delete_response)

        metrics_response = spec["paths"]["/metrics"]["get"]["responses"]["200"]
        self.assertNotIn("application/json", metrics_response.get("content", {}))

    def test_operation_ids_are_unique(self):
        spec = self.live_spec()
        operation_ids = [
            operation["operationId"]
            for path_item in spec["paths"].values()
            for method, operation in path_item.items()
            if method in {"get", "post", "put", "patch", "delete", "head", "options"}
        ]
        self.assertEqual(len(operation_ids), len(set(operation_ids)))

    def test_openapi_response_is_deterministic(self):
        first_status, first_headers, first_body = self.request("GET", "/openapi.json")
        second_status, second_headers, second_body = self.request(
            "GET", "/openapi.json"
        )
        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertIn("application/json", first_headers.get("Content-Type", ""))
        self.assertIn("application/json", second_headers.get("Content-Type", ""))
        self.assertEqual(first_body, second_body)
        document = json.loads(first_body)
        self.assertEqual(document["openapi"], "3.0.3")
        self.assertEqual(document["info"]["title"], "agent-qa")
        self.assertEqual(document["info"]["x-git-sha"], "openapi-test-sha")


if __name__ == "__main__":
    unittest.main()
