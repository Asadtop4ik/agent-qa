import io
import json
import re
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from agent_qa import orders, products
from agent_qa.metrics import MetricsRegistry
from agent_qa.openapi import build_openapi
from agent_qa.routes import ROUTES
from agent_qa.server import Handler


API_KEY = "qa-synthetic-key"


def invoke(method, path, payload=None, api_key=API_KEY):
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


def product_payload(sku, name="Acceptance item", **changes):
    return {
        "sku": sku,
        "name": name,
        "category": "acceptance",
        "price_cents": 100,
        **changes,
    }


def unique_token():
    return uuid.uuid4().hex[:12]


def create_product(test_case, sku, name="Acceptance item", **changes):
    response = invoke("POST", "/products", product_payload(sku, name, **changes))
    test_case.assertEqual(response["status"], 201)
    test_case.assertEqual(
        response["headers"]["Location"], f"/products/{response['body']['id']}"
    )
    return response["body"]


class Task81AcceptanceTests(unittest.TestCase):
    def assert_api_error(self, response, status, code):
        self.assertEqual(response["status"], status)
        self.assertEqual(response["body"]["error"]["code"], code)
        return response["body"]["error"]

    def test_ac1_store_capacity_duplicate_sku_and_never_reused_ids(self):
        store = products.ProductStore()
        self.assertEqual(products.MAX_PRODUCTS, 500)
        first = store.create(**product_payload("STORE-ONE"))
        with self.assertRaises(products.ProductError) as duplicate:
            store.create(**product_payload("STORE-ONE"))
        self.assertEqual(duplicate.exception.code, "duplicate_sku")
        self.assertEqual(duplicate.exception.details[0]["field"], "sku")

        for index in range(499):
            store.create(**product_payload(f"STORE-{index + 2}"))
        with self.assertRaises(products.ProductError) as full:
            store.create(**product_payload("STORE-OVERFLOW"))
        self.assertEqual(full.exception.code, "store_full")
        self.assertTrue(store.delete(first["id"]))
        after_delete = store.create(**product_payload("STORE-AFTER-DELETE"))
        self.assertEqual(after_delete["id"], 501)

    def test_ac1_create_returns_location_and_conflict_codes(self):
        sku = "API-" + unique_token().upper()
        created = create_product(self, sku)
        self.assertEqual(created["sku"], sku)
        duplicate = invoke("POST", "/products", product_payload(sku))
        error = self.assert_api_error(duplicate, 409, "duplicate_sku")
        self.assertEqual(error["details"][0]["field"], "sku")

        unauthorized = invoke("POST", "/products", product_payload("X"), None)
        self.assert_api_error(unauthorized, 401, "unauthorized")

    def test_ac1_post_full_store_returns_store_full(self):
        route = next(
            route
            for route in ROUTES
            if route["method"] == "POST" and route["path"] == "/products"
        )
        handler_globals = route["handler"].__globals__
        existing_stores = {
            name: value
            for name, value in handler_globals.items()
            if isinstance(value, products.ProductStore)
        }
        self.assertTrue(existing_stores)
        full_store = products.ProductStore()
        for index in range(500):
            full_store.create(**product_payload(f"FULL-{index:03d}"))
        replacements = {name: full_store for name in existing_stores}
        with patch.dict(handler_globals, replacements):
            response = invoke("POST", "/products", product_payload("FULL-OVERFLOW"))
        self.assert_api_error(response, 409, "store_full")

    def test_ac1_product_field_constraints_and_defaults(self):
        store = products.ProductStore()
        defaulted = store.create(**product_payload("DEFAULT-ITEM"))
        self.assertEqual(defaulted["stock"], 0)
        self.assertEqual(defaulted["tags"], [])
        self.assertEqual(defaulted["active"], True)

        invalid_payloads = (
            ({"sku": "A"}, "sku"),
            ({"sku": "INVALID SKU"}, "sku"),
            ({"sku": "S" * 33}, "sku"),
            ({"name": ""}, "name"),
            ({"name": " "}, "name"),
            ({"name": "N" * 121}, "name"),
            ({"category": "UPPER"}, "category"),
            ({"category": "c" * 33}, "category"),
            ({"price_cents": -1}, "price_cents"),
            ({"price_cents": 100_000_001}, "price_cents"),
            ({"price_cents": True}, "price_cents"),
            ({"stock": -1}, "stock"),
            ({"stock": 1_000_001}, "stock"),
            ({"stock": True}, "stock"),
            ({"tags": ["tag"] * 11}, "tags"),
            ({"tags": ["UPPER"]}, "tags[0]"),
            ({"tags": ["t" * 25]}, "tags[0]"),
            ({"tags": ["same", "same"]}, None),
            ({"active": 1}, "active"),
        )
        for index, (changes, field) in enumerate(invalid_payloads):
            payload = product_payload(f"INVALID-{index}")
            payload.update(changes)
            with self.subTest(field=field, changes=changes):
                with self.assertRaises(products.ProductError) as error:
                    store.create(**payload)
                self.assertEqual(error.exception.code, "validation_error")
                if field is not None:
                    self.assertIn(
                        field,
                        [detail["field"] for detail in error.exception.details],
                    )

    def test_ac2_filters_search_sort_and_pagination(self):
        suffix = unique_token()
        shared = {"category": f"cat-{suffix}", "tags": [f"tag-{suffix}"]}
        one = create_product(
            self,
            f"A-{suffix.upper()}",
            "Alpha " + suffix,
            **shared,
            price_cents=200,
            stock=2,
            active=True,
        )
        two = create_product(
            self,
            f"B-{suffix.upper()}",
            "Beta " + suffix,
            **shared,
            price_cents=100,
            stock=0,
            active=False,
        )
        three = create_product(
            self,
            f"C-{suffix.upper()}",
            "Gamma other",
            category=f"else-{suffix}",
            tags=[f"tag-{suffix}"],
            price_cents=300,
            stock=1,
            active=True,
        )

        filters = (
            (f"category=cat-{suffix}", [one["id"], two["id"]]),
            (f"tag=tag-{suffix}", [one["id"], two["id"], three["id"]]),
            (f"category=cat-{suffix}&active=true", [one["id"]]),
            (f"category=cat-{suffix}&active=false", [two["id"]]),
            (f"category=cat-{suffix}&in_stock=true", [one["id"]]),
            (f"category=cat-{suffix}&min_price_cents=200", [one["id"]]),
            (f"category=cat-{suffix}&max_price_cents=100", [two["id"]]),
            (f"q={suffix.upper()}", [one["id"], two["id"], three["id"]]),
        )
        for query, expected_ids in filters:
            with self.subTest(query=query):
                response = invoke("GET", f"/products?{query}")
                self.assertEqual(response["status"], 200)
                self.assertEqual(
                    {item["id"] for item in response["body"]["items"]},
                    set(expected_ids),
                )

        for sort in (
            "id",
            "-id",
            "price_cents",
            "-price_cents",
            "name",
            "-name",
            "created_at",
        ):
            with self.subTest(sort=sort):
                response = invoke("GET", f"/products?category=cat-{suffix}&sort={sort}")
                self.assertEqual(response["status"], 200)
                self.assertEqual(response["body"]["total"], 2)
                sort_ids = [item["id"] for item in response["body"]["items"]]
                expected_order = {
                    "id": [one["id"], two["id"]],
                    "-id": [two["id"], one["id"]],
                    "price_cents": [two["id"], one["id"]],
                    "-price_cents": [one["id"], two["id"]],
                    "name": [one["id"], two["id"]],
                    "-name": [two["id"], one["id"]],
                    "created_at": [one["id"], two["id"]],
                }
                self.assertEqual(sort_ids, expected_order[sort])

        tie_category = f"ties-{suffix}"
        tie_one = create_product(
            self,
            f"TIE-A-{suffix.upper()}",
            "Tied item",
            category=tie_category,
            price_cents=400,
        )
        tie_two = create_product(
            self,
            f"TIE-B-{suffix.upper()}",
            "Tied item",
            category=tie_category,
            price_cents=400,
        )
        for sort in ("price_cents", "name", "created_at"):
            tied = invoke("GET", f"/products?category={tie_category}&sort={sort}")
            self.assertEqual(
                [item["id"] for item in tied["body"]["items"]],
                [tie_one["id"], tie_two["id"]],
            )

        page = invoke("GET", f"/products?category=cat-{suffix}&limit=1&offset=1")
        self.assertEqual(page["status"], 200)
        self.assertEqual(page["body"]["limit"], 1)
        self.assertEqual(page["body"]["offset"], 1)
        self.assertEqual(page["body"]["total"], 2)
        self.assertEqual(page["body"]["items"][0]["id"], two["id"])
        self.assertEqual(
            orders.DEFAULT_LIMIT,
            invoke("GET", "/products")["body"]["limit"],
        )

    def test_ac2_invalid_query_reports_invalid_query_and_field(self):
        for query, field in (
            ("unknown=1", "unknown"),
            ("category=a&category=b", "category"),
            ("category=INVALID", "category"),
            ("tag=INVALID", "tag"),
            ("active=yes", "active"),
            ("in_stock=yes", "in_stock"),
            ("min_price_cents=no", "min_price_cents"),
            ("max_price_cents=no", "max_price_cents"),
            ("q=", "q"),
            ("q=" + "x" * 65, "q"),
            ("sort=unknown", "sort"),
            ("limit=0", "limit"),
            ("limit=101", "limit"),
            ("offset=-1", "offset"),
            ("min_price_cents=20&max_price_cents=10", "min_price_cents"),
        ):
            with self.subTest(query=query):
                error = self.assert_api_error(
                    invoke("GET", "/products?" + query), 400, "invalid_query"
                )
                self.assertIn(field, [detail["field"] for detail in error["details"]])

    def test_ac3_get_patch_delete_and_immutable_sku(self):
        suffix = unique_token().upper()
        created = create_product(self, f"PATCH-{suffix}")
        path = f"/products/{created['id']}"
        fetched = invoke("GET", path)
        self.assertEqual(fetched["status"], 200)
        self.assertEqual(fetched["body"]["id"], created["id"])

        changed = invoke("PATCH", path, {"name": "Updated acceptance item"})
        self.assertEqual(changed["status"], 200)
        self.assertEqual(changed["body"]["name"], "Updated acceptance item")
        self.assertNotEqual(changed["body"]["updated_at"], created["updated_at"])

        error = self.assert_api_error(
            invoke("PATCH", path, {"sku": "CHANGED-SKU"}), 400, "validation_error"
        )
        self.assertIn(
            {"field": "sku", "message": "Cannot be changed"}, error["details"]
        )
        deleted = invoke("DELETE", path)
        self.assertEqual(deleted["status"], 204)
        self.assertEqual(deleted["body"], None)
        missing = self.assert_api_error(invoke("GET", path), 404, "product_not_found")
        self.assertEqual(missing["code"], "product_not_found")
        self.assert_api_error(invoke("DELETE", path), 404, "product_not_found")

    def test_ac4_stock_adjustment_is_atomic_and_categories_are_aggregated(self):
        suffix = unique_token()
        store = products.ProductStore()
        item = store.create(
            **product_payload(
                f"RACE-{suffix.upper()}", stock=100, category=f"race-{suffix}"
            )
        )

        def decrement(_):
            try:
                store.adjust_stock(item["id"], -1)
                return "ok"
            except products.ProductError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=200) as executor:
            outcomes = list(executor.map(decrement, range(200)))
        self.assertEqual(store.get(item["id"])["stock"], 0)
        self.assertEqual(outcomes.count("insufficient_stock"), 100)

        api_item = create_product(
            self,
            f"STOCK-{suffix.upper()}",
            "Stock API item",
            category=f"stock-{suffix}",
            stock=1,
        )
        adjusted = invoke(
            "POST", f"/products/{api_item['id']}/adjust-stock", {"delta": 2}
        )
        self.assertEqual(adjusted["status"], 200)
        self.assertEqual(adjusted["body"]["stock"], 3)
        rejected = self.assert_api_error(
            invoke(
                "POST",
                f"/products/{api_item['id']}/adjust-stock",
                {"delta": -4},
            ),
            409,
            "insufficient_stock",
        )
        self.assertEqual(rejected["code"], "insufficient_stock")
        invalid_delta = invoke(
            "POST", f"/products/{api_item['id']}/adjust-stock", {"delta": 0}
        )
        self.assert_api_error(invalid_delta, 400, "validation_error")
        oversized_delta = invoke(
            "POST",
            f"/products/{api_item['id']}/adjust-stock",
            {"delta": 1_000_001},
        )
        self.assert_api_error(oversized_delta, 400, "validation_error")
        current = invoke("GET", f"/products/{api_item['id']}")
        self.assertEqual(current["body"]["stock"], 3)

        category = f"aggregate-{suffix}"
        create_product(
            self,
            f"AGG-A-{suffix.upper()}",
            "Aggregate one",
            category=category,
            price_cents=50,
            stock=4,
            active=True,
        )
        create_product(
            self,
            f"AGG-B-{suffix.upper()}",
            "Aggregate two",
            category=category,
            price_cents=90,
            stock=0,
            active=False,
        )
        categories = invoke("GET", "/categories")
        self.assertEqual(categories["status"], 200)
        self.assertEqual(
            [row["category"] for row in categories["body"]["items"]],
            sorted(row["category"] for row in categories["body"]["items"]),
        )
        aggregate = next(
            row for row in categories["body"]["items"] if row["category"] == category
        )
        self.assertEqual(
            aggregate,
            {
                "category": category,
                "products": 2,
                "active_products": 1,
                "in_stock": 1,
                "min_price_cents": 50,
                "max_price_cents": 90,
            },
        )

    def test_ac5_metrics_optional_gauge_and_openapi_route_drift(self):
        registry = MetricsRegistry()
        empty = registry.render(0, "acceptance-sha")
        self.assertNotIn("agent_qa_products", empty)
        populated = registry.render(0, "acceptance-sha", 3)
        self.assertIn("# TYPE agent_qa_products gauge", populated)
        self.assertIn("agent_qa_products 3", populated)
        create_product(self, f"METRICS-{unique_token().upper()}")
        metrics_response = invoke("GET", "/metrics")
        self.assertEqual(metrics_response["status"], 200)
        self.assertIn("agent_qa_products", metrics_response["body"])

        expected = {
            ("POST", "/products"),
            ("GET", "/products"),
            ("GET", "/products/{id}"),
            ("PATCH", "/products/{id}"),
            ("DELETE", "/products/{id}"),
            ("POST", "/products/{id}/adjust-stock"),
            ("GET", "/categories"),
        }
        route_pairs = {(route["method"], route["path"]) for route in ROUTES}
        self.assertTrue(expected.issubset(route_pairs))
        spec = build_openapi(ROUTES, "acceptance-sha")
        spec_pairs = {
            (method.upper(), path)
            for path, operations in spec["paths"].items()
            for method in operations
        }
        self.assertTrue(expected.issubset(spec_pairs))
        for method, path in sorted(expected):
            route = next(
                route
                for route in ROUTES
                if route["path"] == path and route["method"] == method
            )
            operation = spec["paths"][path][method.lower()]
            self.assertEqual(operation["operationId"], route["operation_id"])
            self.assertIn("responses", operation)
            if route["auth_required"]:
                self.assertEqual(operation["security"], [{"ApiKeyAuth": []}])

        responses = spec["paths"]["/products"]["post"]["responses"]
        for status in ("401", "409", "413", "415"):
            self.assertIn(status, responses)
        detail_responses = spec["paths"]["/products/{id}"]["get"]["responses"]
        self.assertIn("404", detail_responses)
        stock_responses = spec["paths"]["/products/{id}/adjust-stock"]["post"][
            "responses"
        ]
        for status in ("401", "404", "409", "413", "415"):
            self.assertIn(status, stock_responses)
        create_schema = spec["paths"]["/products"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        self.assertEqual(
            create_schema["properties"]["sku"]["pattern"],
            "^[A-Z0-9][A-Z0-9-]{1,31}$",
        )
        self.assertEqual(create_schema["properties"]["name"]["minLength"], 1)
        self.assertEqual(create_schema["properties"]["name"]["maxLength"], 120)
        self.assertEqual(
            create_schema["properties"]["category"]["pattern"],
            "^[a-z0-9][a-z0-9-]{0,31}$",
        )
        self.assertEqual(create_schema["properties"]["price_cents"]["minimum"], 0)
        self.assertEqual(
            create_schema["properties"]["price_cents"]["maximum"], 100_000_000
        )
        self.assertEqual(create_schema["properties"]["stock"]["default"], 0)
        self.assertEqual(create_schema["properties"]["stock"]["minimum"], 0)
        self.assertEqual(create_schema["properties"]["stock"]["maximum"], 1_000_000)
        self.assertEqual(create_schema["properties"]["tags"]["minItems"], 0)
        self.assertEqual(create_schema["properties"]["tags"]["maxItems"], 10)
        self.assertEqual(
            create_schema["properties"]["tags"]["items"]["pattern"],
            "^[a-z0-9-]{1,24}$",
        )
        self.assertEqual(create_schema["properties"]["active"]["default"], True)
        parameters = {
            parameter["name"]: parameter["schema"]
            for parameter in spec["paths"]["/products"]["get"]["parameters"]
        }
        self.assertEqual(
            parameters["sort"]["enum"],
            ["id", "-id", "price_cents", "-price_cents", "name", "-name", "created_at"],
        )
        self.assertEqual(parameters["limit"]["minimum"], orders.MIN_LIMIT)
        self.assertEqual(parameters["limit"]["maximum"], orders.MAX_LIMIT)
        self.assertEqual(parameters["limit"]["default"], orders.DEFAULT_LIMIT)
        self.assertEqual(parameters["offset"]["minimum"], orders.MIN_OFFSET)
        self.assertEqual(parameters["offset"]["default"], orders.DEFAULT_OFFSET)
        self.assertTrue(Path("tests/test_products_store.py").is_file())
        self.assertTrue(Path("tests/test_products_api.py").is_file())
        readme = Path("README.md").read_text(encoding="utf-8")
        self.assertIsNotNone(
            re.search(
                r"^#{2,6} .*?(?:products?|mahsulotlar?)",
                readme,
                re.IGNORECASE | re.MULTILINE,
            )
        )


if __name__ == "__main__":
    unittest.main()
