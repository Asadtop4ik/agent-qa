"""In-memory HTTP adapter tests for the product endpoints."""

import io
import json
import unittest
import uuid
from unittest.mock import patch

from agent_qa.products import ProductStore
from agent_qa.server import Handler


API_KEY = "api-test-key"


def invoke(
    method,
    path,
    payload=None,
    api_key=API_KEY,
    raw_body=None,
    content_type="application/json",
):
    encoded = raw_body
    if encoded is None:
        encoded = b"" if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Length": str(len(encoded)), "Content-Type": content_type}
    if api_key is not None:
        headers["X-API-Key"] = api_key
    response = {}
    handler = object.__new__(Handler)
    handler.command = method
    handler.path = path
    handler.headers = headers
    handler.rfile = io.BytesIO(encoded)
    handler.request_id = "product-api-test"
    handler._json = lambda status, body, extra=None: response.update(
        status=status, body=body, headers=extra or {}
    )
    handler._handle()
    return response


def make_product(name="Test product", **changes):
    return {
        "sku": "T-" + uuid.uuid4().hex[:12].upper(),
        "name": name,
        "category": "test-category",
        "price_cents": 125,
        **changes,
    }


class ProductsApiTests(unittest.TestCase):
    def setUp(self):
        self.store_patch = patch("agent_qa.routes.PRODUCT_STORE", ProductStore())
        self.auth_patch = patch(
            "agent_qa.server.is_valid_api_key",
            side_effect=lambda headers: headers.get("X-API-Key") == API_KEY,
        )
        self.store_patch.start()
        self.auth_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.addCleanup(self.auth_patch.stop)

    def create(self, payload=None):
        response = invoke("POST", "/products", payload or make_product())
        self.assertEqual(response["status"], 201)
        self.assertEqual(
            response["headers"]["Location"],
            f"/products/{response['body']['id']}",
        )
        return response["body"]

    def error(self, response, status, code):
        self.assertEqual(response["status"], status)
        self.assertEqual(response["body"]["error"]["code"], code)
        return response["body"]["error"]

    def test_filters_search_sort_and_pagination(self):
        token = uuid.uuid4().hex[:8]
        category = "needle-" + token
        first = self.create(
            make_product(
                "Alpha " + token,
                category=category,
                tags=["blue"],
                stock=2,
                price_cents=200,
            )
        )
        second = self.create(
            make_product(
                "Beta " + token,
                category=category,
                tags=["red"],
                stock=0,
                active=False,
                price_cents=100,
            )
        )
        third = self.create(
            make_product(
                "Gamma other",
                category="other-" + token,
                tags=["blue"],
                stock=1,
                price_cents=300,
            )
        )

        filters = (
            (f"category={category}", [first, second]),
            ("tag=blue", [first, third]),
            (f"category={category}&active=true", [first]),
            (f"category={category}&active=false", [second]),
            (f"category={category}&in_stock=true", [first]),
            (f"category={category}&in_stock=false", [second]),
            (f"category={category}&min_price_cents=200", [first]),
            (f"category={category}&max_price_cents=100", [second]),
            (f"q={token.upper()}", [first, second]),
            (f"q={first['sku'].lower()}", [first]),
            ("q=BLUE", [first, third]),
        )
        for query, expected in filters:
            with self.subTest(query=query):
                response = invoke("GET", "/products?" + query)
                self.assertEqual(response["status"], 200)
                self.assertEqual(
                    [item["id"] for item in response["body"]["items"]],
                    [item["id"] for item in expected],
                )

        expected_sorts = {
            "id": [first, second, third],
            "-id": [third, second, first],
            "price_cents": [second, first, third],
            "-price_cents": [third, first, second],
            "name": [first, second, third],
            "-name": [third, second, first],
            "created_at": [first, second, third],
        }
        for sort, expected in expected_sorts.items():
            with self.subTest(sort=sort):
                response = invoke("GET", f"/products?sort={sort}")
                self.assertEqual(
                    [item["id"] for item in response["body"]["items"]],
                    [item["id"] for item in expected],
                )

        page = invoke("GET", "/products?limit=1&offset=1")
        self.assertEqual(page["body"]["total"], 3)
        self.assertEqual(page["body"]["limit"], 1)
        self.assertEqual(page["body"]["offset"], 1)
        self.assertEqual(page["body"]["items"][0]["id"], second["id"])

        same_time = "2026-01-01T00:00:00Z"
        with patch("agent_qa.products._timestamp", return_value=same_time):
            tied_first = self.create(make_product("Tied", price_cents=400))
            tied_second = self.create(make_product("Tied", price_cents=400))
        for sort in ("price_cents", "name", "created_at"):
            with self.subTest(tied_sort=sort):
                tied = invoke("GET", f"/products?sort={sort}&q=Tied")
                self.assertEqual(
                    [item["id"] for item in tied["body"]["items"]],
                    [tied_first["id"], tied_second["id"]],
                )

    def test_categories_and_stock_adjustment(self):
        token = uuid.uuid4().hex[:8]
        category = "aggregate-" + token
        first = self.create(
            make_product(category=category, price_cents=50, stock=4, active=True)
        )
        self.create(
            make_product(category=category, price_cents=90, stock=0, active=False)
        )
        result = invoke("GET", "/categories")
        self.assertEqual(result["status"], 200)
        self.assertEqual(
            [row["category"] for row in result["body"]["items"]],
            sorted(row["category"] for row in result["body"]["items"]),
        )
        aggregate = next(
            row for row in result["body"]["items"] if row["category"] == category
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

        path = f"/products/{first['id']}/adjust-stock"
        adjusted = invoke("POST", path, {"delta": 2})
        self.assertEqual(adjusted["status"], 200)
        self.assertEqual(adjusted["body"]["stock"], 6)
        rejected = self.error(
            invoke("POST", path, {"delta": -7}), 409, "insufficient_stock"
        )
        self.assertEqual(rejected["code"], "insufficient_stock")
        invalid = self.error(
            invoke("POST", path, {"delta": 0}), 400, "validation_error"
        )
        self.assertEqual(invalid["details"][0]["field"], "delta")
        maxed = self.create(make_product(stock=1_000_000))
        overflow = self.error(
            invoke("POST", f"/products/{maxed['id']}/adjust-stock", {"delta": 1}),
            400,
            "validation_error",
        )
        self.assertIn("stock", [detail["field"] for detail in overflow["details"]])
        current = invoke("GET", f"/products/{maxed['id']}")
        self.assertEqual(current["body"]["stock"], 1_000_000)

    def test_validation_duplicate_sku_and_query_errors(self):
        payload = make_product()
        created = self.create(payload)
        duplicate = self.error(
            invoke("POST", "/products", payload), 409, "duplicate_sku"
        )
        self.assertEqual(duplicate["details"][0]["field"], "sku")
        self.assertEqual(invoke("GET", f"/products/{created['id']}")["status"], 200)

        invalid_payloads = (
            ({"sku": "A"}, "sku"),
            ({"name": ""}, "name"),
            ({"category": "UPPER"}, "category"),
            ({"price_cents": True}, "price_cents"),
            ({"price_cents": 100_000_001}, "price_cents"),
            ({"stock": True}, "stock"),
            ({"stock": 1_000_001}, "stock"),
            ({"tags": ["same", "same"]}, "tags"),
            ({"active": 1}, "active"),
            ({"extra": 1}, "extra"),
        )
        for changes, field in invalid_payloads:
            body = make_product(**changes)
            with self.subTest(invalid_field=field):
                error = self.error(
                    invoke("POST", "/products", body), 400, "validation_error"
                )
                self.assertIn(field, [detail["field"] for detail in error["details"]])

        invalid_queries = (
            ("unknown=1", "unknown"),
            ("category=a&category=b", "category"),
            ("active=yes", "active"),
            ("in_stock=yes", "in_stock"),
            ("min_price_cents=no", "min_price_cents"),
            ("max_price_cents=no", "max_price_cents"),
            ("min_price_cents=20&max_price_cents=10", "min_price_cents"),
            ("q=", "q"),
            ("q=" + "x" * 65, "q"),
            ("sort=unknown", "sort"),
            ("limit=0", "limit"),
            ("limit=101", "limit"),
            ("offset=-1", "offset"),
            ("offset=" + "9" * 129, "offset"),
        )
        for query, field in invalid_queries:
            with self.subTest(query=query):
                error = self.error(
                    invoke("GET", "/products?" + query), 400, "invalid_query"
                )
                self.assertIn(field, [detail["field"] for detail in error["details"]])

    def test_patch_delete_authentication_404_body_limits_and_allow_headers(self):
        item = self.create()
        path = f"/products/{item['id']}"
        patched = invoke("PATCH", path, {"name": "Renamed"})
        self.assertEqual(patched["status"], 200)
        self.assertEqual(patched["body"]["name"], "Renamed")
        self.assertNotEqual(patched["body"]["updated_at"], item["updated_at"])

        sku_error = self.error(
            invoke("PATCH", path, {"sku": "CHANGED-SKU"}), 400, "validation_error"
        )
        self.assertIn(
            {"field": "sku", "message": "Cannot be changed"}, sku_error["details"]
        )
        empty_patch = self.error(invoke("PATCH", path, {}), 400, "validation_error")
        self.assertTrue(empty_patch["details"])

        self.assertEqual(invoke("GET", path)["status"], 200)
        deleted = invoke("DELETE", path)
        self.assertEqual(deleted["status"], 204)
        self.assertIsNone(deleted["body"])
        self.error(invoke("GET", path), 404, "product_not_found")
        self.error(
            invoke("PATCH", path, {"name": "Missing"}),
            404,
            "product_not_found",
        )
        self.error(invoke("DELETE", path), 404, "product_not_found")
        self.error(
            invoke("POST", path + "/adjust-stock", {"delta": 1}),
            404,
            "product_not_found",
        )

        for method, endpoint in (
            ("POST", "/products"),
            ("PATCH", path),
            ("DELETE", path),
            ("POST", path + "/adjust-stock"),
        ):
            with self.subTest(unauthorized_method=method, endpoint=endpoint):
                self.error(
                    invoke(method, endpoint, {} if method != "DELETE" else None, None),
                    401,
                    "unauthorized",
                )

        too_large = self.error(
            invoke(
                "POST",
                "/products",
                raw_body=b"x" * 4097,
            ),
            413,
            "payload_too_large",
        )
        self.assertEqual(too_large["code"], "payload_too_large")
        self.error(
            invoke("POST", "/products", {}, content_type="text/plain"),
            415,
            "unsupported_media_type",
        )

        for endpoint, expected in (
            ("/products", "GET, POST"),
            (path, "DELETE, GET, PATCH"),
            ("/categories", "GET"),
        ):
            with self.subTest(allow_path=endpoint):
                response = invoke("PUT", endpoint)
                self.assertEqual(response["status"], 405)
                self.assertEqual(response["headers"]["Allow"], expected)


if __name__ == "__main__":
    unittest.main()
