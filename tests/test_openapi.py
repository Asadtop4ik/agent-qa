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

from agent_qa import orders
from agent_qa import schemas
from agent_qa.openapi import build_openapi
from agent_qa.routes import ROUTES
from agent_qa.schemas import SCHEMAS


ROOT = Path(__file__).resolve().parents[1]
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


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
        return template.replace("{id}", "1")

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

    def test_named_schemas_are_published_and_schema_routes_are_public(self):
        spec = build_openapi(ROUTES, "synthetic-sha")
        self.assertEqual(spec["components"]["schemas"], SCHEMAS)
        for path, method in (
            ("/schemas", "get"),
            ("/schemas/{name}", "get"),
            ("/schemas/{name}/validate", "post"),
        ):
            with self.subTest(path=path):
                self.assertNotIn("security", spec["paths"][path][method])
        validate_operation = spec["paths"]["/schemas/{name}/validate"]["post"]
        request_schema = validate_operation["requestBody"]["content"][
            "application/json"
        ]["schema"]
        self.assertEqual(request_schema, {})
        self.assertIn("400", validate_operation["responses"])
        self.assertEqual(
            validate_operation["responses"]["400"]["content"]["application/json"][
                "schema"
            ]["$ref"],
            "#/components/schemas/Error",
        )
        get_schema_operation = spec["paths"]["/schemas/{name}"]["get"]
        self.assertEqual(
            get_schema_operation["responses"]["404"]["content"]["application/json"][
                "schema"
            ]["$ref"],
            "#/components/schemas/Error",
        )

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

        create_schema = spec["paths"]["/orders"]["post"]["requestBody"]
        create_schema = create_schema["content"]["application/json"]["schema"]
        customer_schema = create_schema["properties"]["customer_id"]
        total_schema = create_schema["properties"]["total_cents"]
        self.assertEqual(customer_schema["minLength"], orders.MIN_CUSTOMER_ID_LENGTH)
        self.assertEqual(customer_schema["maxLength"], orders.MAX_CUSTOMER_ID_LENGTH)
        self.assertEqual(total_schema["minimum"], orders.MIN_TOTAL_CENTS)
        self.assertEqual(total_schema["maximum"], orders.MAX_TOTAL_CENTS)

        patch_schema = spec["paths"]["/orders/{id}"]["patch"]["requestBody"]
        patch_schema = patch_schema["content"]["application/json"]["schema"]
        patch_total = patch_schema["properties"]["total_cents"]
        patch_status = patch_schema["properties"]["status"]["enum"]
        self.assertEqual(patch_total["minimum"], orders.MIN_TOTAL_CENTS)
        self.assertEqual(patch_total["maximum"], orders.MAX_TOTAL_CENTS)
        self.assertEqual(patch_status, list(orders.STATUSES))

    def test_product_schemas_and_query_limits_come_from_constants(self):
        spec = self.live_spec()
        create_schema = spec["paths"]["/products"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        self.assertEqual(create_schema, schemas.SCHEMAS["CreateProduct"])
        self.assertEqual(
            create_schema["properties"]["sku"]["pattern"], schemas.SKU_PATTERN
        )
        self.assertEqual(
            create_schema["properties"]["category"]["pattern"],
            schemas.CATEGORY_PATTERN,
        )
        self.assertEqual(
            create_schema["properties"]["price_cents"]["maximum"],
            schemas.MAX_PRICE_CENTS,
        )
        self.assertEqual(
            create_schema["properties"]["stock"]["maximum"], schemas.MAX_STOCK
        )
        self.assertEqual(
            create_schema["properties"]["tags"]["maxItems"], schemas.MAX_TAGS
        )

        parameters = {
            parameter["name"]: parameter["schema"]
            for parameter in spec["paths"]["/products"]["get"]["parameters"]
        }
        self.assertEqual(parameters["category"]["pattern"], schemas.CATEGORY_PATTERN)
        self.assertEqual(parameters["tag"]["pattern"], schemas.TAG_PATTERN)
        self.assertEqual(parameters["sort"]["enum"], list(schemas.PRODUCT_SORTS))
        self.assertEqual(parameters["limit"]["minimum"], schemas.MIN_LIMIT)
        self.assertEqual(parameters["limit"]["maximum"], schemas.MAX_LIMIT)
        self.assertEqual(parameters["limit"]["default"], schemas.DEFAULT_LIMIT)
        self.assertEqual(parameters["offset"]["minimum"], schemas.MIN_OFFSET)
        self.assertEqual(parameters["offset"]["default"], schemas.DEFAULT_OFFSET)

    def test_product_operations_document_auth_errors_and_schemas(self):
        spec = self.live_spec()
        operations = {
            (route["path"], route["method"].lower()): route for route in ROUTES
        }
        for path, method in (
            ("/products", "post"),
            ("/products", "get"),
            ("/products/{id}", "get"),
            ("/products/{id}", "patch"),
            ("/products/{id}", "delete"),
            ("/products/{id}/adjust-stock", "post"),
            ("/categories", "get"),
        ):
            with self.subTest(path=path, method=method):
                operation = spec["paths"][path][method]
                route = operations[(path, method)]
                self.assertEqual(operation["operationId"], route["operation_id"])
                self.assertEqual(
                    bool(operation.get("security")), route["auth_required"]
                )
                for status in route["responses"]:
                    self.assertIn(status, operation["responses"])

        create = spec["paths"]["/products"]["post"]
        for status in ("401", "409", "413", "415"):
            self.assertIn(status, create["responses"])
        stock = spec["paths"]["/products/{id}/adjust-stock"]["post"]
        for status in ("401", "404", "409", "413", "415"):
            self.assertIn(status, stock["responses"])
        patch_schema = spec["paths"]["/products/{id}"]["patch"]["requestBody"][
            "content"
        ]["application/json"]["schema"]
        self.assertEqual(patch_schema, schemas.SCHEMAS["UpdateProduct"])

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
