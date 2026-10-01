"""Socket-free tests for the product route handlers and HTTP dispatcher."""

import hashlib
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from io import BytesIO
from unittest.mock import patch
from urllib.parse import quote

from agent_qa import schemas
from agent_qa.errors import ApiError
from agent_qa.openapi import build_openapi
from agent_qa.products import ProductStore
from agent_qa.config import GIT_SHA
from agent_qa.ratelimit import TokenBucketLimiter
from agent_qa.routes import ROUTES, search_products
from agent_qa.server import Handler, allowed_methods


MAX_STOCK = schemas.MAX_STOCK


AUTH_IDENTITY = {"key_id": "test", "role": "admin", "label": "test", "tenants": None}


class ProductApiTests(unittest.TestCase):
    def setUp(self):
        self.store = ProductStore()
        self.store_patch = patch("agent_qa.routes.PRODUCT_STORE", self.store)
        self.store_patch.start()
        self.auth_patch = patch(
            "agent_qa.server.authenticate_api_key", return_value=AUTH_IDENTITY
        )
        self.auth_patch.start()
        self.rate_limiter_patch = patch(
            "agent_qa.server.RATE_LIMITER", TokenBucketLimiter()
        )
        self.rate_limiter_patch.start()

    def tearDown(self):
        self.rate_limiter_patch.stop()
        self.auth_patch.stop()
        self.store_patch.stop()

    @staticmethod
    def dispatch(
        method, path, payload=None, content_type="application/json", extra_headers=None
    ):
        raw = json.dumps(payload).encode("utf-8") if payload is not None else b""
        headers = Message()
        if payload is not None:
            headers["Content-Length"] = str(len(raw))
            headers["Content-Type"] = content_type
        for name, value in (extra_headers or {}).items():
            headers[name] = value
        responses = []
        handler = object.__new__(Handler)
        handler.path = path
        handler.command = method
        handler.headers = headers
        handler.rfile = BytesIO(raw)
        handler.request_id = "products-test"
        handler._json = lambda *args: responses.append(args)
        try:
            Handler._dispatch(handler)
        except ApiError as error:
            return (
                error.status,
                {
                    "error": {
                        "code": error.code,
                        "message": error.message,
                        "details": error.details,
                    }
                },
                {"ETag": error.current_etag}
                if getattr(error, "current_etag", None)
                else {},
            )
        status, body, response_headers = responses[0]
        return status, body, response_headers

    def create(self, sku="SKU-1", **changes):
        payload = {
            "sku": sku,
            "name": "Widget",
            "category": "tools",
            "price_cents": 1200,
            "stock": 3,
            "tags": ["sale"],
            "active": True,
        }
        payload.update(changes)
        return self.dispatch("POST", "/products", payload)

    def test_route_table_has_authenticated_write_routes_and_expected_responses(self):
        expected = {
            ("POST", "/products"),
            ("POST", "/products/bulk"),
            ("GET", "/products"),
            ("GET", "/products/search"),
            ("GET", "/products/{id}"),
            ("PATCH", "/products/{id}"),
            ("DELETE", "/products/{id}"),
            ("POST", "/products/{id}/adjust-stock"),
            ("GET", "/categories"),
        }
        product_routes = {
            (route["method"], route["path"])
            for route in ROUTES
            if route["path"].startswith("/products") or route["path"] == "/categories"
        }
        self.assertEqual(product_routes, expected)
        response_codes = {
            (route["method"], route["path"]): set(route["responses"])
            for route in ROUTES
        }
        for method, path in (
            ("POST", "/products"),
            ("POST", "/products/bulk"),
            ("PATCH", "/products/{id}"),
            ("DELETE", "/products/{id}"),
            ("POST", "/products/{id}/adjust-stock"),
        ):
            self.assertTrue(
                next(
                    route["auth_required"]
                    for route in ROUTES
                    if (route["method"], route["path"]) == (method, path)
                )
            )
        self.assertTrue(
            {"401", "409", "413", "415"}.issubset(response_codes[("POST", "/products")])
        )
        self.assertTrue(
            {"201", "207", "401", "409", "413", "415", "422"}.issubset(
                response_codes[("POST", "/products/bulk")]
            )
        )
        self.assertTrue(
            {"401", "404", "409", "413", "415"}.issubset(
                response_codes[("PATCH", "/products/{id}")]
            )
        )
        self.assertTrue(
            {"401", "404"}.issubset(response_codes[("DELETE", "/products/{id}")])
        )
        self.assertTrue(
            {"401", "404", "409", "413", "415"}.issubset(
                response_codes[("POST", "/products/{id}/adjust-stock")]
            )
        )
        self.assertIn("404", response_codes[("GET", "/products/{id}")])
        self.assertEqual(allowed_methods("/products"), "GET, POST")
        self.assertEqual(allowed_methods("/products/1"), "DELETE, GET, PATCH")
        self.assertEqual(allowed_methods("/products/1/adjust-stock"), "POST")

    def test_search_returns_filtered_offset_page_and_query_summary(self):
        self.create("SKU-1", name="Widget")
        self.create("SKU-2", name="Gadget", active=False)
        status, body, _ = self.dispatch(
            "GET", "/products/search?q=active%3Dtrue&sort=-id&limit=1&offset=0"
        )
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"items", "total", "limit", "offset", "query"})
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["limit"], 1)
        self.assertEqual(body["offset"], 0)
        self.assertEqual(body["items"][0]["sku"], "SKU-1")
        self.assertEqual(body["query"], {"normalized": "active = true", "terms": 1})

    def test_search_operators_compounds_date_and_offset_sorting(self):
        self.create("SKU-1", name="Widget", price_cents=1000, tags=["featured"])
        self.create(
            "SKU-2",
            name="Gadget",
            category="books",
            active=False,
            price_cents=2000,
            tags=["clearance"],
        )
        self.create(
            "SKU-3",
            name="Book",
            category="books",
            price_cents=2000,
            tags=["featured"],
        )
        cases = (
            ("price_cents:1000", {"SKU-1"}),
            ("price_cents!=1000", {"SKU-2", "SKU-3"}),
            ("price_cents>1000", {"SKU-2", "SKU-3"}),
            ("price_cents>=2000", {"SKU-2", "SKU-3"}),
            ("price_cents<2000", {"SKU-1"}),
            ("price_cents<=1000", {"SKU-1"}),
            ("name~wid", {"SKU-1"}),
            ("tags IN (featured, clearance)", {"SKU-1", "SKU-2", "SKU-3"}),
            (
                "active=true AND (category:tools OR category:books) NOT active=false",
                {"SKU-1", "SKU-3"},
            ),
        )
        for expression, expected_skus in cases:
            with self.subTest(expression=expression):
                status, body, _ = self.dispatch(
                    "GET", "/products/search?q=" + quote(expression, safe="")
                )
                self.assertEqual(status, 200)
                self.assertEqual({item["sku"] for item in body["items"]}, expected_skus)

        date = min(item["created_at"][:10] for item in self.store.list()[0])
        status, body, _ = self.dispatch(
            "GET", f"/products/search?q=created_at%3E%3D{date}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 3)
        status, page, _ = self.dispatch(
            "GET", "/products/search?q=price_cents%3D2000&sort=-price_cents&offset=1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(page["items"][0]["sku"], "SKU-3")

    def test_search_rejects_unknown_duplicate_missing_and_malformed_query(self):
        for path, code, field in (
            ("/products/search?unknown=x&q=id%3D1", "invalid_query", "q"),
            ("/products/search?q=id%3D1&q=id%3D2", "invalid_query", "q"),
            ("/products/search", "invalid_query", "q"),
            ("/products/search?q=id%3D1&sort=nope", "invalid_query", "sort"),
            (
                "/products/search?q=id%3D1&limit=99999999999999999999",
                "invalid_query",
                "limit",
            ),
            (
                "/products/search?q=id%3D1&offset=99999999999999999999",
                "invalid_query",
                "offset",
            ),
            ("/products/search?q=id%3D1&limit=abc", "invalid_query", "limit"),
            ("/products/search?q=", "invalid_search_query", "q"),
            ("/products/search?q=totl_cents%3D1", "invalid_search_query", "q"),
            ("/products/search?q=" + "x" * 501, "invalid_search_query", "q"),
        ):
            with self.subTest(path=path):
                status, body, _ = self.dispatch("GET", path)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], code)
                self.assertEqual(body["error"]["details"][0]["field"], field)
                if code == "invalid_search_query":
                    position = "500" if len(path.split("=", 1)[1]) > 500 else "0"
                    self.assertEqual(body["error"]["details"][0]["position"], position)

    def test_search_malformed_direct_query_input_is_a_400(self):
        for query in (None, [("q", None)], [("q",)], "q=id%3D1"):
            with self.subTest(query=query), self.assertRaises(ApiError) as error:
                search_products(query)
            self.assertEqual(error.exception.status, 400)
            self.assertEqual(error.exception.code, "invalid_query")

    def test_search_explain_checks_resource_and_returns_ast(self):
        status, body, _ = self.dispatch(
            "GET", "/search/explain?resource=products&q=active%3Dtrue"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            {
                "resource": "products",
                "normalized": "active = true",
                "ast": {"field": "active", "op": "=", "value": True},
            },
        )
        status, repeated, _ = self.dispatch(
            "GET", "/search/explain?resource=products&q=active%3Dtrue"
        )
        self.assertEqual(status, 200)
        self.assertEqual(repeated, body)
        for path in (
            "/search/explain?resource=products",
            "/search/explain?q=active%3Dtrue",
            "/search/explain?resource=products&resource=orders&q=id%3D1",
            "/search/explain?resource=products&q=id%3D1&q=id%3D2",
        ):
            with self.subTest(path=path):
                status, error, _ = self.dispatch("GET", path)
                self.assertEqual(status, 400)
                self.assertEqual(error["error"]["code"], "invalid_query")
                self.assertEqual(error["error"]["details"][0]["field"], "q")
        status, body, _ = self.dispatch(
            "GET", "/search/explain?resource=inventory&q=id%3D1"
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_query")

    def test_create_duplicate_sku_and_capacity_errors(self):
        status, product, headers = self.create()
        self.assertEqual(status, 201)
        self.assertEqual(headers["Location"], f"/products/{product['id']}")
        status, error, _ = self.create()
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "duplicate_sku")
        self.assertEqual(error["error"]["details"][0]["field"], "sku")

    def test_create_bounds_defaults_and_full_store_response(self):
        status, created, _ = self.dispatch(
            "POST",
            "/products",
            {"sku": "AA", "name": "A", "category": "a", "price_cents": 0},
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["stock"], 0)
        self.assertEqual(created["tags"], [])
        self.assertTrue(created["active"])

        for index in range(499):
            self.store.create(
                sku=f"SKU-{index}",
                name="Widget",
                category="tools",
                price_cents=1,
                stock=0,
                tags=[],
                active=True,
            )
        status, body, _ = self.dispatch(
            "POST",
            "/products",
            {"sku": "FULL-1", "name": "Full", "category": "tools", "price_cents": 1},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "store_full")

    def test_create_and_patch_reject_invalid_product_fields(self):
        bad_products = (
            {"sku": "a-1", "name": "Widget", "category": "tools", "price_cents": 1},
            {"sku": "A", "name": "Widget", "category": "tools", "price_cents": 1},
            {"sku": "AA", "name": "  ", "category": "tools", "price_cents": 1},
            {"sku": "AA", "name": "x" * 121, "category": "tools", "price_cents": 1},
            {"sku": "AA", "name": "Widget", "category": "Bad", "price_cents": 1},
            {"sku": "AA", "name": "Widget", "category": "tools", "price_cents": True},
            {
                "sku": "AA",
                "name": "Widget",
                "category": "tools",
                "price_cents": 1,
                "stock": 1_000_001,
            },
            {
                "sku": "AA",
                "name": "Widget",
                "category": "tools",
                "price_cents": 1,
                "tags": ["sale", "sale"],
            },
            {
                "sku": "AA",
                "name": "Widget",
                "category": "tools",
                "price_cents": 1,
                "tags": ["!bad"],
            },
            {
                "sku": "AA",
                "name": "Widget",
                "category": "tools",
                "price_cents": 1,
                "tags": ["t"] * 11,
            },
        )
        for payload in bad_products:
            with self.subTest(payload=payload):
                status, body, _ = self.dispatch("POST", "/products", payload)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "validation_error")

        _, product, _ = self.create()
        product_id = product["id"]
        for payload in (
            {},
            {"stock": True},
            {"category": "Bad"},
            {"sku": None},
            {"sku": 5},
        ):
            with self.subTest(patch=payload):
                status, body, _ = self.dispatch(
                    "PATCH", f"/products/{product_id}", payload
                )
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "validation_error")
        for payload in (None, {}, {"delta": 0}, {"delta": MAX_STOCK + 1}):
            with self.subTest(adjustment=payload):
                status, body, _ = self.dispatch(
                    "POST", f"/products/{product_id}/adjust-stock", payload
                )
                self.assertEqual(status, 411 if payload is None else 400)
                self.assertEqual(
                    body["error"]["code"],
                    "length_required" if payload is None else "validation_error",
                )

    def test_each_filter_and_crossed_filter_sort_pagination(self):
        self.create(
            "ALPHA-1",
            name="alpha",
            category="tools",
            price_cents=900,
            stock=3,
            tags=["red", "sale"],
        )
        self.create(
            "BETA-1",
            name="Beta",
            category="tools",
            price_cents=900,
            stock=0,
            tags=["blue"],
            active=False,
        )
        self.create(
            "GAMMA-1",
            name="ALPHA",
            category="books",
            price_cents=500,
            stock=2,
            tags=["sale"],
        )
        self.create(
            "DELTA-1",
            name="delta",
            category="books",
            price_cents=1500,
            stock=10,
            tags=["red"],
        )

        expected = {
            "category=tools": ["ALPHA-1", "BETA-1"],
            "tag=sale": ["ALPHA-1", "GAMMA-1"],
            "active=true": ["ALPHA-1", "GAMMA-1", "DELTA-1"],
            "active=false": ["BETA-1"],
            "in_stock=true": ["ALPHA-1", "GAMMA-1", "DELTA-1"],
            "in_stock=false": ["BETA-1"],
            "min_price_cents=900": ["ALPHA-1", "BETA-1", "DELTA-1"],
            "max_price_cents=900": ["ALPHA-1", "BETA-1", "GAMMA-1"],
            "min_price_cents=500&max_price_cents=900": [
                "ALPHA-1",
                "BETA-1",
                "GAMMA-1",
            ],
            "q=lph": ["ALPHA-1", "GAMMA-1"],
            "q=beta-1": ["BETA-1"],
            "q=RED": ["ALPHA-1", "DELTA-1"],
        }
        for query, skus in expected.items():
            with self.subTest(query=query):
                status, result, _ = self.dispatch("GET", f"/products?{query}")
                self.assertEqual(status, 200)
                self.assertEqual([item["sku"] for item in result["items"]], skus)
                self.assertEqual(result["total"], len(skus))

        sort_expected = {
            "id": ["ALPHA-1", "BETA-1", "GAMMA-1", "DELTA-1"],
            "-id": ["DELTA-1", "GAMMA-1", "BETA-1", "ALPHA-1"],
            "price_cents": ["GAMMA-1", "ALPHA-1", "BETA-1", "DELTA-1"],
            "-price_cents": ["DELTA-1", "ALPHA-1", "BETA-1", "GAMMA-1"],
            "name": ["GAMMA-1", "BETA-1", "ALPHA-1", "DELTA-1"],
            "-name": ["DELTA-1", "ALPHA-1", "BETA-1", "GAMMA-1"],
            "created_at": ["ALPHA-1", "BETA-1", "GAMMA-1", "DELTA-1"],
        }
        for sort, products in sort_expected.items():
            with self.subTest(sort=sort):
                status, result, _ = self.dispatch("GET", f"/products?sort={sort}")
                actual = [item["sku"] for item in result["items"]]
                self.assertEqual(status, 200)
                self.assertEqual(actual, products)

        status, page, _ = self.dispatch(
            "GET", "/products?category=tools&sort=-price_cents&limit=1&offset=1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["items"][0]["sku"], "BETA-1")
        self.assertEqual((page["limit"], page["offset"]), (1, 1))
        status, page, _ = self.dispatch("GET", "/products?limit=2&offset=2")
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["sku"] for item in page["items"]], ["GAMMA-1", "DELTA-1"]
        )
        self.assertEqual(page["total"], 4)
        self.assertEqual(page["limit"], 2)
        status, page, _ = self.dispatch("GET", "/products?limit=1&offset=9")
        self.assertEqual(status, 200)
        self.assertEqual(page["items"], [])
        self.assertEqual(page["total"], 4)
        self.assertEqual((page["limit"], page["offset"]), (1, 9))
        status, page, _ = self.dispatch("GET", "/products")
        self.assertEqual(status, 200)
        self.assertEqual((page["limit"], page["offset"]), (20, 0))

        status, page, headers = self.dispatch(
            "GET",
            "/products?category=tools&sort=-price_cents&limit=1&pagination=cursor",
        )
        self.assertEqual(status, 200)
        self.assertEqual(set(page), {"items", "total", "limit", "next_cursor"})
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["items"][0]["sku"], "ALPHA-1")
        self.assertTrue(headers["Link"].startswith("</products?"))
        next_path = headers["Link"].split("<", 1)[1].split(">", 1)[0]
        self.assertIn("category=tools", next_path)
        self.assertIn("sort=-price_cents", next_path)
        self.assertIn("limit=1", next_path)
        status, second_page, second_headers = self.dispatch("GET", next_path)
        self.assertEqual(status, 200)
        self.assertEqual(second_page["items"][0]["sku"], "BETA-1")
        self.assertIsNone(second_page["next_cursor"])
        self.assertNotIn("Link", second_headers)

    def test_invalid_and_duplicate_query_parameters_are_400(self):
        invalid_queries = (
            ("unknown=x", "unknown"),
            ("active=yes", "active"),
            ("active=TRUE", "active"),
            ("in_stock=1", "in_stock"),
            ("category=Bad", "category"),
            ("tag=bad!", "tag"),
            ("sort=unknown", "sort"),
            ("limit=0", "limit"),
            ("limit=101", "limit"),
            ("offset=-1", "offset"),
            ("pagination=cursor&offset=20", "cursor"),
            ("pagination=cursor&offset=0", "cursor"),
            ("cursor=abc&offset=1", "cursor"),
            ("pagination=offset&cursor=abc", "cursor"),
            ("offset=" + "9" * 5000, "offset"),
            ("min_price_cents=100000001", "min_price_cents"),
            ("max_price_cents=bad", "max_price_cents"),
            ("min_price_cents=100&max_price_cents=10", "min_price_cents"),
            ("q=", "q"),
            ("q=" + "x" * 65, "q"),
            ("limit=999999999999999999999999999999999999999999", "limit"),
            ("tag=a&tag=b", "tag"),
            ("active=true&active=false", "active"),
        )
        for query, field in invalid_queries:
            with self.subTest(query=query):
                status, body, _ = self.dispatch("GET", f"/products?{query}")
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "invalid_query")
                self.assertTrue(body["error"]["details"])
                self.assertTrue(
                    any(item["field"] == field for item in body["error"]["details"])
                )

    def test_cursor_rejects_tampering_and_filter_changes(self):
        for conflict in ("", "&offset=0", "&pagination=offset"):
            status, body, _ = self.dispatch(
                "GET", "/products?cursor=" + "x" * 4097 + conflict
            )
            self.assertEqual(status, 400)
            self.assertEqual(
                body["error"]["code"],
                "invalid_query" if conflict else "invalid_cursor",
            )
        self.create("CURSOR-1", category="tools")
        self.create("CURSOR-2", category="tools")
        status, page, headers = self.dispatch(
            "GET", "/products?category=tools&limit=1&pagination=cursor"
        )
        self.assertEqual(status, 200)
        cursor = page["next_cursor"]
        self.assertIsNotNone(cursor)
        tampered = cursor[:-1] + ("0" if cursor[-1] != "0" else "1")
        status, body, _ = self.dispatch("GET", "/products?cursor=" + tampered)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_cursor")

        next_path = headers["Link"].split("<", 1)[1].split(">", 1)[0]
        status, body, _ = self.dispatch("GET", next_path.replace("tools", "books"))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "cursor_mismatch")

    def test_cursor_link_preserves_boolean_filters_as_lowercase(self):
        self.create("ACTIVE-1", active=True, stock=4)
        self.create("ACTIVE-2", active=True, stock=2)
        status, page, headers = self.dispatch(
            "GET",
            "/products?active=true&in_stock=true&limit=1&pagination=cursor",
        )
        self.assertEqual(status, 200)
        self.assertEqual(page["total"], 2)
        self.assertIn("active=true", headers["Link"])
        self.assertIn("in_stock=true", headers["Link"])
        self.assertNotIn("active=True", headers["Link"])
        self.assertNotIn("in_stock=True", headers["Link"])

        next_path = headers["Link"].split("<", 1)[1].split(">", 1)[0]
        status, next_page, _ = self.dispatch("GET", next_path)
        self.assertEqual(status, 200)
        self.assertEqual(next_page["total"], 2)
        self.assertEqual(len(next_page["items"]), 1)
        self.assertIsNone(next_page["next_cursor"])

    def test_dispatch_reports_405_413_and_415_for_product_routes(self):
        status, _, headers = self.dispatch("PUT", "/products")
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "GET, POST")

        status, body, _ = self.dispatch("POST", "/products", {"name": "x" * 5000})
        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "payload_too_large")

        status, body, _ = self.dispatch(
            "POST", "/products", {"name": "Widget"}, content_type="text/plain"
        )
        self.assertEqual(status, 415)
        self.assertEqual(body["error"]["code"], "unsupported_media_type")

    def test_get_patch_sku_immutable_and_delete(self):
        _, product, _ = self.create()
        product_id = product["id"]
        self.assertEqual(self.dispatch("GET", f"/products/{product_id}")[0], 200)
        status, patched, _ = self.dispatch(
            "PATCH", f"/products/{product_id}", {"name": "Updated"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(patched["name"], "Updated")
        status, error, _ = self.dispatch(
            "PATCH", f"/products/{product_id}", {"sku": "NEW"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["details"][0]["field"], "sku")
        self.assertEqual(error["error"]["details"][0]["message"], "Cannot be changed")
        status, body, headers = self.dispatch("DELETE", f"/products/{product_id}")
        self.assertEqual(status, 204)
        self.assertIsNone(body)
        self.assertEqual(headers["Content-Length"], "0")
        self.assertEqual(self.dispatch("GET", f"/products/{product_id}")[0], 404)
        self.assertEqual(self.dispatch("GET", "/products/9" + "9" * 5000)[0], 404)

    def test_adjust_stock_and_categories(self):
        _, product, _ = self.create()
        product_id = product["id"]
        status, updated, _ = self.dispatch(
            "POST", f"/products/{product_id}/adjust-stock", {"delta": -2}
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["stock"], 1)
        status, error, _ = self.dispatch(
            "POST", f"/products/{product_id}/adjust-stock", {"delta": -2}
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "insufficient_stock")
        self.assertEqual(self.store.get(product_id)["stock"], 1)

        overflow_status, overflow, _ = self.dispatch(
            "POST",
            f"/products/{product_id}/adjust-stock",
            {"delta": MAX_STOCK},
        )
        self.assertEqual(overflow_status, 400)
        self.assertEqual(overflow["error"]["code"], "validation_error")
        self.assertEqual(self.store.get(product_id)["stock"], 1)

        missing = (
            ("GET", "/products/999", None),
            ("PATCH", "/products/999", {"name": "Missing"}),
            ("DELETE", "/products/999", None),
            ("POST", "/products/999/adjust-stock", {"delta": 1}),
        )
        for method, path, payload in missing:
            with self.subTest(method=method, path=path):
                status, body, _ = self.dispatch(method, path, payload)
                self.assertEqual(status, 404)
                self.assertEqual(body["error"]["code"], "product_not_found")

        self.create("BOOK-2", category="books", stock=0, active=False, price_cents=500)
        status, result, _ = self.dispatch("GET", "/categories")
        self.assertEqual(status, 200)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["items"][0]["category"], "books")
        self.assertEqual(result["items"][0]["products"], 1)
        self.assertEqual(result["items"][0]["active_products"], 0)
        self.assertEqual(result["items"][0]["in_stock"], 0)
        self.assertEqual(result["items"][0]["min_price_cents"], 500)
        self.assertEqual(result["items"][0]["max_price_cents"], 500)
        self.assertEqual(result["items"][1]["products"], 1)
        self.assertEqual(result["items"][1]["in_stock"], 1)
        self.assertEqual(result["items"][1]["min_price_cents"], 1200)
        self.assertEqual(result["items"][1]["max_price_cents"], 1200)

    def test_authentication_covers_mutating_product_endpoints(self):
        self.auth_patch.stop()
        try:
            for method, path, payload in (
                ("POST", "/products", {"sku": "A1"}),
                ("PATCH", "/products/1", {"name": "x"}),
                ("DELETE", "/products/1", None),
                ("POST", "/products/1/adjust-stock", {"delta": 1}),
            ):
                with self.subTest(method=method, path=path):
                    status, body, _ = self.dispatch(method, path, payload)
                    self.assertEqual(status, 401)
        finally:
            self.auth_patch.start()

    def test_metrics_exposes_product_count_and_ready_response_is_unchanged(self):
        self.create()
        status, metrics, _ = self.dispatch("GET", "/metrics")
        self.assertEqual(status, 200)
        self.assertIn("# TYPE agent_qa_products gauge", metrics)
        self.assertIn("agent_qa_products 1", metrics)

        status, ready, _ = self.dispatch("GET", "/ready")
        self.assertEqual(status, 200)
        self.assertEqual(ready, {"status": "ready", "git_sha": GIT_SHA})

    def test_openapi_document_uses_product_route_metadata_and_shared_schemas(self):
        from agent_qa.schemas import SCHEMAS

        document = build_openapi(ROUTES, "products-test")
        self.assertEqual(document["components"]["schemas"], SCHEMAS)
        paths = document["paths"]
        for path, method in (
            ("/products", "post"),
            ("/products", "get"),
            ("/products/search", "get"),
            ("/search/explain", "get"),
            ("/products/{id}", "get"),
            ("/products/{id}", "patch"),
            ("/products/{id}", "delete"),
            ("/products/{id}/adjust-stock", "post"),
            ("/categories", "get"),
        ):
            self.assertIn(method, paths[path])
        search_parameters = {
            parameter["name"]: parameter
            for parameter in paths["/products/search"]["get"]["parameters"]
        }
        self.assertEqual(search_parameters["q"]["schema"]["maxLength"], 500)
        self.assertIn("active (bool)", search_parameters["q"]["description"])
        self.assertEqual(
            search_parameters["sort"]["schema"]["enum"],
            list(schemas.PRODUCT_SORTS),
        )
        self.assertEqual(
            paths["/products"]["post"]["requestBody"]["content"]["application/json"][
                "schema"
            ],
            SCHEMAS["CreateProduct"],
        )
        self.assertEqual(
            paths["/products/{id}/adjust-stock"]["post"]["responses"].keys(),
            {
                "200",
                "400",
                "401",
                "403",
                "404",
                "406",
                "409",
                "411",
                "412",
                "413",
                "415",
                "428",
                "429",
                "503",
            },
        )

    def test_product_etags_and_if_match_dispatch(self):
        _, product, created_headers = self.create()
        created_etag = created_headers["ETag"]
        self.assertNotIn("version", product)
        path = f"/products/{product['id']}"

        status, current, headers = self.dispatch("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["ETag"], created_etag)
        status, listing, headers = self.dispatch("GET", "/products")
        canonical = json.dumps(
            listing, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()[:16]
        self.assertEqual(headers["ETag"], f'W/"{digest}"')
        status, body, headers = self.dispatch(
            "GET", "/products", extra_headers={"If-None-Match": headers["ETag"]}
        )
        self.assertEqual(status, 304)
        self.assertIsNone(body)
        status, body, headers = self.dispatch(
            "GET", "/products", extra_headers={"If-None-Match": f'"{digest}"'}
        )
        self.assertEqual(status, 304)
        self.assertIsNone(body)
        status, body, headers = self.dispatch(
            "GET", "/products", extra_headers={"If-None-Match": "*"}
        )
        self.assertEqual(status, 304)
        self.assertIsNone(body)
        self.assertIn("ETag", headers)

        status, error, headers = self.dispatch(
            "PATCH", path, {"name": "Changed"}, extra_headers={"If-Match": '"stale"'}
        )
        self.assertEqual(status, 412)
        self.assertEqual(error["error"]["code"], "precondition_failed")
        self.assertEqual(headers["ETag"], created_etag)
        self.assertEqual(self.store.get(product["id"])["name"], "Widget")
        status, error, _ = self.dispatch(
            "PATCH",
            path,
            {"name": "Changed"},
            extra_headers={"If-Match": f"W/{created_etag}"},
        )
        self.assertEqual(status, 412)
        self.assertEqual(error["error"]["code"], "precondition_failed")
        status, error, _ = self.dispatch(
            "PATCH", path, {"name": "Changed"}, extra_headers={"If-Match": "p1.1"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "invalid_precondition")
        status, error, _ = self.dispatch(
            "PATCH", path, {}, extra_headers={"If-Match": "not-an-etag"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "validation_error")
        status, updated, headers = self.dispatch(
            "PATCH",
            path,
            {"name": "Changed"},
            extra_headers={"If-Match": f'"unrelated", {created_etag}'},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["ETag"], created_etag.rsplit(".", 1)[0] + '.2"')

    def test_stock_and_delete_preconditions_preserve_state_and_error_order(self):
        _, product, headers = self.create()
        path = f"/products/{product['id']}"
        before = self.store.get(product["id"], include_version=True)
        for method, target, payload in (
            ("POST", path + "/adjust-stock", {"delta": -1}),
            ("DELETE", path, None),
        ):
            with self.subTest(method=method):
                status, error, current_headers = self.dispatch(
                    method, target, payload, extra_headers={"If-Match": '"stale"'}
                )
                self.assertEqual(status, 412)
                self.assertEqual(error["error"]["code"], "precondition_failed")
                self.assertEqual(current_headers["ETag"], headers["ETag"])
                self.assertEqual(
                    self.store.get(product["id"], include_version=True), before
                )
        status, error, _ = self.dispatch(
            "PATCH",
            "/products/999",
            {"name": "Changed"},
            extra_headers={"If-Match": "malformed"},
        )
        self.assertEqual(status, 404)
        with patch.dict("os.environ", {"AGENT_QA_REQUIRE_IF_MATCH": "true"}):
            status, error, _ = self.dispatch("PATCH", path, {"name": "Changed"})
        self.assertEqual(status, 428)
        self.assertEqual(error["error"]["code"], "precondition_required")

    def test_same_if_match_allows_exactly_one_concurrent_patch(self):
        _, product, created_headers = self.create()
        expected = created_headers["ETag"]

        def update(index):
            from agent_qa.routes import patch_product

            try:
                return patch_product(
                    [],
                    {"id": str(product["id"])},
                    {"name": f"Concurrent {index}"},
                    request_headers={"If-Match": expected},
                )[0]
            except ApiError as error:
                return error.status

        with ThreadPoolExecutor(max_workers=20) as executor:
            statuses = list(executor.map(update, range(20)))
        self.assertEqual(statuses.count(200), 1)
        self.assertEqual(statuses.count(412), 19)


if __name__ == "__main__":
    unittest.main()
