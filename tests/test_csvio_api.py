"""Socket-free HTTP coverage for CSV export and import routes."""

import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_qa import routes
from agent_qa.orders import OrderStore
from agent_qa.products import ProductStore
from agent_qa.server import Handler
from tests.test_content_negotiation_handler import make_handler


class CsvApiTests(unittest.TestCase):
    def setUp(self):
        self.products = ProductStore()
        self.orders = OrderStore()
        patches = (
            patch.object(routes, "PRODUCT_STORE", self.products),
            patch.object(routes, "ORDER_STORE", self.orders),
            patch(
                "agent_qa.server.RATE_LIMITER.consume",
                return_value=SimpleNamespace(
                    limit=100, remaining=99, reset_after=1, allowed=True
                ),
            ),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def request(
        self, method, path, body=None, content_type=None, headers=None, role="write"
    ):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        request_headers = dict(headers or {})
        if raw is not None:
            request_headers["Content-Length"] = str(len(raw))
        if content_type:
            request_headers["Content-Type"] = content_type
        handler = make_handler(path, method, request_headers)
        handler.rfile = io.BytesIO(raw or b"")
        identity = {"key_id": "csv-test", "role": role} if role else None
        with patch("agent_qa.server.authenticate_api_key", return_value=identity):
            Handler._handle(handler)
        return handler.status, dict(handler.sent_headers), handler.wfile.getvalue()

    def test_export_negotiation_and_header_only_csv(self):
        status, headers, body = self.request("GET", "/exports/products.csv")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/csv; charset=utf-8")
        self.assertEqual(
            headers["Content-Disposition"], 'attachment; filename="products.csv"'
        )
        self.assertEqual(
            body,
            (
                b"id,sku,name,category,price_cents,stock,tags,active,"
                b"created_at,updated_at\r\n"
            ),
        )

        status, _, body = self.request(
            "GET", "/exports/orders.csv", headers={"Accept": "application/json"}
        )
        self.assertEqual(status, 406)
        self.assertEqual(json.loads(body)["error"]["code"], "not_acceptable")

    def test_import_media_type_size_and_csv_bom(self):
        status, _, _ = self.request(
            "POST", "/imports/products", "{}", "application/json"
        )
        self.assertEqual(status, 415)

        status, _, _ = self.request(
            "POST", "/imports/products", "x" * 70000, "text/csv"
        )
        self.assertEqual(status, 413)

        csv = "\ufeffsku,name,category,price_cents\r\nCSV-BOM,Widget,tools,10\r\n"
        status, _, body = self.request(
            "POST",
            "/imports/products?mode=validate",
            csv,
            "text/csv; charset=utf-8",
        )
        self.assertEqual(status, 200)
        report = json.loads(body)
        self.assertEqual(report["rows"], 1)
        self.assertEqual(report["created"], 0)
        self.assertFalse(report["applied"])

    def test_import_abort_skip_and_structural_row_limit(self):
        csv = (
            "sku,name,category,price_cents\r\n"
            "CSV-SKIP,Widget,tools,10\r\n"
            "CSV-BAD,Widget,Bad,10\r\n"
        )
        status, _, body = self.request("POST", "/imports/products", csv, "text/csv")
        self.assertEqual(status, 422)
        report = json.loads(body)
        self.assertEqual(report["created"], 0)
        self.assertFalse(report["applied"])
        self.assertEqual(set(report["errors"][0]), {"line", "field", "message"})

        status, _, body = self.request(
            "POST", "/imports/products?on_error=skip", csv, "text/csv"
        )
        self.assertEqual(status, 201)
        report = json.loads(body)
        self.assertEqual(report["created"], 1)
        self.assertEqual(report["failed"], 1)
        self.assertTrue(report["applied"])

        head = "sku,name,category,price_cents\r\n"
        rows = "".join(f"CSV-{index:03d},Widget,tools,10\r\n" for index in range(200))
        status, _, _ = self.request(
            "POST", "/imports/products?mode=validate", head + rows, "text/csv"
        )
        self.assertEqual(status, 200)
        status, _, body = self.request(
            "POST",
            "/imports/products?mode=validate",
            head + rows + "CSV-200,Widget,tools,10\r\n",
            "text/csv",
        )
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["code"], "invalid_csv")
        self.assertEqual(error["details"][0]["field"], "body")

    def test_product_export_filters_and_round_trip(self):
        self.products.create(
            sku="CSV-1",
            name='=text,"quote"\nnext',
            category="tools",
            price_cents=10,
            stock=2,
            tags=["one", "two"],
        )
        self.products.create(
            sku="CSV-2", name="Other", category="other", price_cents=20, active=False
        )
        path = "/exports/products.csv?category=tools&active=true&in_stock=true&q=text"
        status, _, exported = self.request("GET", path, role=None)
        self.assertEqual(status, 200)
        self.assertIn(b"'=text", exported)
        self.assertNotIn(b"CSV-2", exported)
        with patch.object(routes, "PRODUCT_STORE", ProductStore()) as imported:
            status, _, body = self.request(
                "POST", "/imports/products", exported, "text/csv"
            )
            self.assertEqual(status, 201)
            self.assertEqual(json.loads(body)["created"], 1)
            expected, actual = self.products.get(1), imported.get(1)
            for field in (
                "sku",
                "name",
                "category",
                "price_cents",
                "stock",
                "tags",
                "active",
            ):
                self.assertEqual(actual[field], expected[field])

    def test_order_export_round_trip_and_import_policies(self):
        csv = "customer_id,total_cents\r\n=customer,50\r\ncustomer,-1\r\n"
        for policy in ("abort", "skip"):
            status, _, body = self.request(
                "POST",
                f"/imports/orders?mode=validate&on_error={policy}",
                csv,
                "text/csv",
            )
            report = json.loads(body)
            self.assertEqual(status, 200)
            self.assertEqual(report["failed"], 1)
            self.assertEqual(report["created"], 0)
            self.assertFalse(report["applied"])
        status, _, body = self.request("POST", "/imports/orders", csv, "text/csv")
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(body)["created"], 0)
        status, _, body = self.request(
            "POST", "/imports/orders?on_error=skip", csv, "text/csv"
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["created"], 1)
        self.orders.create("other", 100)
        status, headers, exported = self.request(
            "GET", "/exports/orders.csv?status=new&customer_id=%3Dcustomer", role=None
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            headers["Content-Disposition"], 'attachment; filename="orders.csv"'
        )
        self.assertIn(b"'=customer", exported)
        self.assertNotIn(b"other", exported)
        with patch.object(routes, "ORDER_STORE", OrderStore()) as imported:
            status, _, _ = self.request("POST", "/imports/orders", exported, "text/csv")
            self.assertEqual(status, 201)
            self.assertEqual(imported.get(1)["customer_id"], "=customer")
            self.assertEqual(imported.get(1)["total_cents"], 50)
        status, _, body = self.request(
            "POST",
            "/imports/orders?on_error=skip",
            "customer_id,total_cents\r\ncustomer,-1\r\n",
            "text/csv",
        )
        self.assertEqual(status, 422)
        self.assertFalse(json.loads(body)["applied"])

    def test_auth_query_charset_and_structural_errors(self):
        csv = "customer_id,total_cents\r\ncustomer,10\r\n"
        for role, expected in ((None, 401), ("read", 403)):
            status, _, _ = self.request(
                "POST", "/imports/orders", csv, "text/csv", role=role
            )
            self.assertEqual(status, expected)
        for path in (
            "/exports/orders.csv?unknown=1",
            "/exports/products.csv?limit=1",
            "/exports/products.csv?active=maybe",
            "/exports/orders.csv?status=new&status=new",
            "/imports/orders?mode=invalid",
            "/imports/orders?on_error=invalid",
        ):
            method = "POST" if path.startswith("/imports") else "GET"
            status, _, body = self.request(method, path, csv, "text/csv")
            self.assertEqual(status, 400)
            self.assertEqual(json.loads(body)["error"]["code"], "invalid_query")
        for charset in ("latin-1", "utf8"):
            status, _, _ = self.request(
                "POST", "/imports/orders", csv, f"text/csv; charset={charset}"
            )
            self.assertEqual(status, 415)
        status, _, body = self.request("POST", "/imports/orders", b"\xff", "text/csv")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_csv")
        for text, field in (
            ("", "body"),
            ("customer_id,total_cents\r\n", "body"),
            ("customer_id,unknown\r\n", "header"),
            ("customer_id,customer_id,total_cents\r\n", "header"),
            ("customer_id,total_cents\r\na\r\n", "body"),
        ):
            status, _, body = self.request("POST", "/imports/orders", text, "text/csv")
            self.assertEqual(status, 400)
            error = json.loads(body)["error"]
            self.assertEqual(error["code"], "invalid_csv")
            self.assertEqual(error["details"][0]["field"], field)

    def test_ready_response_is_unchanged(self):
        status, _, body = self.request("GET", "/ready", role=None)
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {"status": "ready", "git_sha": routes.GIT_SHA},
        )
