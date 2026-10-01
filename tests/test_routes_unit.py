import json
import os
import subprocess
import sys
import tempfile
import unittest
from email.message import Message
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

from agent_qa.config import GIT_SHA
from agent_qa.errors import ApiError, envelope
from agent_qa.orders import OrderStore
from agent_qa.routes import (
    ROUTES,
    fixture,
    health,
    list_orders,
    list_products,
    ping,
    ready,
    status,
    version,
)
from agent_qa.schemas import SCHEMAS


AUTH_IDENTITY = {"key_id": "test", "role": "admin", "label": "test"}


class RouteUnitTests(unittest.TestCase):
    def test_cursor_list_handlers_emit_relative_encoded_next_links(self):
        order_filters = {
            "status": "new",
            "customer_id": "id with space",
            "limit": 2,
            "sort": "-id",
            "pagination": "cursor",
        }
        product_filters = {
            "q": "red mug",
            "limit": 3,
            "sort": "-price_cents",
            "pagination": "cursor",
        }
        with (
            patch("agent_qa.routes.validate_query", return_value=order_filters),
            patch(
                "agent_qa.routes.ORDER_STORE.list",
                return_value=([{"id": 1}], 4, "opaque.order"),
            ),
        ):
            status_code, body, headers = list_orders([])
        self.assertEqual(status_code, 200)
        self.assertEqual(set(body), {"items", "total", "limit", "next_cursor"})
        self.assertEqual(body["next_cursor"], "opaque.order")
        self.assertEqual(
            headers["Link"],
            (
                "</orders?status=new&customer_id=id%20with%20space&limit=2&"
                'sort=-id&pagination=cursor&cursor=opaque.order>; rel="next"'
            ),
        )

        with (
            patch(
                "agent_qa.routes.validate_product_query", return_value=product_filters
            ),
            patch(
                "agent_qa.routes.PRODUCT_STORE.list",
                return_value=([{"id": 2}], 5, "opaque.product"),
            ),
        ):
            status_code, body, headers = list_products([])
        self.assertEqual(status_code, 200)
        self.assertEqual(set(body), {"items", "total", "limit", "next_cursor"})
        self.assertIn("q=red%20mug", headers["Link"])
        self.assertIn("sort=-price_cents", headers["Link"])
        self.assertIn("cursor=opaque.product", headers["Link"])

    def test_order_cursor_handler_integrates_validation_store_and_link_traversal(self):
        from urllib.parse import quote

        from agent_qa.server import Handler

        store = OrderStore()
        customer_id = "route+customer&one"
        created = [
            store.create(customer_id, total_cents=100 + index) for index in range(3)
        ]

        def dispatch(path):
            responses = []
            handler = object.__new__(Handler)
            handler.path = path
            handler.command = "GET"
            handler.headers = Message()
            handler.rfile = BytesIO()
            handler.request_id = "orders-route-test"
            handler._json = lambda *args: responses.append(args)
            Handler._handle(handler)
            response = responses[0]
            return (*response, {}) if len(response) == 2 else response

        with patch("agent_qa.routes.ORDER_STORE", store):
            empty_status, empty_body, _ = dispatch(
                "/orders?customer_id=missing&pagination=cursor"
            )
            self.assertEqual(empty_status, 200)
            self.assertEqual(empty_body["items"], [])
            self.assertEqual(empty_body["total"], 0)
            self.assertIsNone(empty_body["next_cursor"])
            self.assertNotIn("offset", empty_body)

            offset_status, offset_body, _ = dispatch(
                "/orders?customer_id="
                + quote(customer_id, safe="")
                + "&limit=2&offset=0&sort=-id"
            )
            self.assertEqual(offset_status, 200)
            self.assertEqual(set(offset_body), {"items", "total", "limit", "offset"})
            self.assertEqual([item["id"] for item in offset_body["items"]], [3, 2])

            first_status, first_body, first_headers = dispatch(
                "/orders?customer_id="
                + quote(customer_id, safe="")
                + "&limit=1&sort=-id&pagination=cursor"
            )
            self.assertEqual(first_status, 200)
            self.assertEqual(
                set(first_body), {"items", "total", "limit", "next_cursor"}
            )
            self.assertEqual(first_body["items"][0]["id"], created[2]["id"])
            link = first_headers["Link"]
            self.assertIn("customer_id=route%2Bcustomer%26one", link)
            next_path = link.split("<", 1)[1].split(">", 1)[0]

            second_status, second_body, _ = dispatch(
                next_path.replace("limit=1", "limit=2")
            )
            self.assertEqual(second_status, 200)
            self.assertEqual([item["id"] for item in second_body["items"]], [2, 1])
            self.assertIsNone(second_body["next_cursor"])

            for changed_query in (
                next_path.replace("route%2Bcustomer%26one", "different"),
                next_path.replace("sort=-id", "sort=id"),
            ):
                status, body, _ = dispatch(changed_query)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "cursor_mismatch")

            for cursor in ("forged", "x" * 4097):
                status, body, _ = dispatch("/orders?cursor=" + cursor)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "invalid_cursor")
                for conflict in ("offset=0", "pagination=offset"):
                    status, body, _ = dispatch(f"/orders?cursor={cursor}&{conflict}")
                    self.assertEqual(status, 400)
                    self.assertEqual(body["error"]["code"], "invalid_query")
                    self.assertTrue(
                        any(
                            item["field"] == "cursor"
                            for item in body["error"]["details"]
                        )
                    )

    def test_only_requested_post_routes_are_idempotent(self):
        flagged = {
            (route["method"], route["path"])
            for route in ROUTES
            if route.get("idempotent")
        }
        self.assertEqual(
            flagged,
            {
                ("POST", "/orders"),
                ("POST", "/products"),
                ("POST", "/orders/bulk"),
                ("POST", "/products/bulk"),
            },
        )

    def test_health_handler_and_route(self):
        code, body, headers = health([])
        self.assertEqual(code, 200)
        self.assertEqual(body, {"status": "ok"})
        self.assertEqual(headers, {})

        route = next(route for route in ROUTES if route["path"] == "/health")
        self.assertEqual(route["method"], "GET")
        self.assertIs(route["handler"], health)
        self.assertEqual(route["responses"], ["200", "403"])

    def test_ping_handler(self):
        status, body, headers = ping([])
        self.assertEqual(status, 200)
        self.assertEqual(body, {"pong": True})
        self.assertEqual(headers, {})

    def test_ready_handler(self):
        status, body, headers = ready([])
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ready", "git_sha": GIT_SHA})
        self.assertEqual(headers, {})

    def test_about_handler(self):
        route = next(route for route in ROUTES if route["path"] == "/about")
        status, body, headers = route["handler"]([])
        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            {"service": "agent-qa", "git_sha": GIT_SHA, "environment": "qa"},
        )
        self.assertEqual(headers, {"X-Service": "agent-qa"})

    def test_status_handler_with_valid_fixture(self):
        code, body, headers = status([])
        self.assertEqual(code, 200)
        self.assertEqual(
            body,
            {"status": "ok", "service": "agent-qa", "checks": {"fixture": True}},
        )
        self.assertEqual(headers, {})

    def test_status_handler_with_invalid_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture_path = Path(directory) / "invalid.json"
            fixture_path.write_text("{invalid json", encoding="utf-8")
            with patch("agent_qa.routes.FIXTURE_PATH", fixture_path):
                code, body, headers = status([])

        self.assertEqual(code, 200)
        self.assertEqual(
            body,
            {
                "status": "degraded",
                "service": "agent-qa",
                "checks": {"fixture": False},
            },
        )
        self.assertEqual(headers, {})

    def test_fixture_handler_and_projection(self):
        status, body, headers = fixture([])
        self.assertEqual(status, 200)
        self.assertEqual(body["record_type"], "synthetic_customer_fixture")
        self.assertEqual(headers, {})

        status, body, _ = fixture([("fields", "name,plan")])
        self.assertEqual(status, 200)
        self.assertEqual(body, {"name": "Example QA Customer", "plan": "sandbox"})

    def test_fixture_query_validation(self):
        invalid_queries = (
            ([("fields", "")], "Fields must not be empty"),
            ([("fields", "name,,plan")], "Fields must not be empty"),
            ([("fields", "nope")], "Unknown field: nope"),
            (
                [("fields", "name"), ("fields", "plan")],
                "The fields parameter may appear once",
            ),
            ([("x", "1")], "Unsupported query parameter: x"),
        )
        for query, message in invalid_queries:
            with self.subTest(query=query), self.assertRaises(ApiError) as error:
                fixture(query)
            self.assertEqual(error.exception.status, 400)
            self.assertEqual(error.exception.code, "invalid_query")
            self.assertEqual(error.exception.message, message)
            self.assertEqual(
                error.exception.details,
                [
                    {
                        "param": "fields" if query[0][0] == "fields" else "x",
                        "message": message,
                    }
                ],
            )

    def test_version_handler(self):
        status, body, headers = version([])
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "agent-qa")
        self.assertEqual(body["git_sha"], GIT_SHA)
        self.assertTrue(body["python_version"])
        self.assertEqual(headers, {})

    def test_api_error_envelope(self):
        message = "Fields must not be empty"
        details = [{"param": "fields", "message": message}]
        error = ApiError(400, "invalid_query", message, details)
        self.assertEqual(
            envelope(error.code, error.message, error.details),
            {
                "error": {
                    "code": "invalid_query",
                    "message": "Fields must not be empty",
                    "details": [
                        {"param": "fields", "message": "Fields must not be empty"}
                    ],
                }
            },
        )

    def test_handler_serializes_fulfillment_api_errors_without_a_socket(self):
        from agent_qa.server import Handler

        handler, responses = self.make_dispatcher("/orders", b"{}")
        details = [
            {"field": "items[2].quantity", "message": "Only 0 in stock"},
            {"field": "items[10].quantity", "message": "Only 0 in stock"},
        ]

        def dispatch_error():
            raise ApiError(409, "insufficient_stock", "Insufficient stock", details)

        Handler._handle(handler, dispatch_error)
        self.assertEqual(responses[0][0], 409)
        self.assertEqual(responses[0][1]["error"]["code"], "insufficient_stock")
        self.assertEqual(responses[0][1]["error"]["details"], details)

    def test_json_304_is_empty_and_omits_content_type_and_length(self):
        from agent_qa.server import Handler

        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                handler = object.__new__(Handler)
                handler.command = method
                handler.request_id = "conditional-test"
                handler.wfile = BytesIO()
                sent_headers = []
                handler._record_response = lambda status: None
                handler.send_response = lambda status: None
                handler.send_header = lambda name, value: sent_headers.append(
                    (name, value)
                )
                handler.end_headers = lambda: None
                Handler._json(handler, 304, None, {"ETag": '"o1.1"'})

                header_names = [name for name, _ in sent_headers]
                self.assertNotIn("Content-Type", header_names)
                self.assertNotIn("Content-Length", header_names)
                self.assertIn(("X-Request-Id", "conditional-test"), sent_headers)
                self.assertIn(("ETag", '"o1.1"'), sent_headers)
                self.assertEqual(handler.wfile.getvalue(), b"")

    def test_order_id_rejects_unbounded_numeric_path(self):
        from agent_qa.routes import _order_id

        self.assertIsNone(_order_id({"id": "9" * 5000}))

    def test_allow_methods_come_from_routes(self):
        from agent_qa.server import allowed_methods

        paths = {route["path"] for route in ROUTES}
        for path in paths:
            with self.subTest(path=path):
                methods = dict.fromkeys(
                    route["method"] for route in ROUTES if route["path"] == path
                )
                self.assertEqual(allowed_methods(path), ", ".join(sorted(methods)))

    def test_literal_bulk_route_precedes_order_id_template(self):
        from agent_qa.server import Handler, _path_routes, allowed_methods

        self.assertEqual(
            [(route["path"], params) for route, params in _path_routes("/orders/bulk")],
            [("/orders/bulk", {})],
        )
        self.assertEqual(allowed_methods("/orders/bulk"), "POST")
        for method in ("GET", "PUT", "DELETE"):
            handler, responses = self.make_dispatcher("/orders/bulk", b"", method)
            Handler._dispatch(handler)
            self.assertEqual(responses[0][0], 405)
            self.assertEqual(responses[0][2]["Allow"], "POST")

        handler, _ = self.make_dispatcher("/orders/1", b"", "GET")
        with self.assertRaises(ApiError) as error:
            Handler._dispatch(handler)
        self.assertEqual(error.exception.status, 404)
        self.assertEqual(error.exception.code, "order_not_found")

    def test_dispatch_uses_route_specific_json_body_limit(self):
        from agent_qa.server import Handler

        body = b'{"items":[]}' + b" " * 5000
        handler, _ = self.make_dispatcher("/orders/bulk", body)
        with patch("agent_qa.server.authenticate_api_key", return_value=AUTH_IDENTITY):
            with self.assertRaises(ApiError) as error:
                Handler._dispatch(handler)
        self.assertEqual(error.exception.status, 400)
        self.assertEqual(error.exception.code, "validation_error")

        handler, _ = self.make_dispatcher("/orders/bulk", b" " * 65537)
        with patch("agent_qa.server.authenticate_api_key", return_value=AUTH_IDENTITY):
            with self.assertRaises(ApiError) as error:
                Handler._dispatch(handler)
        self.assertEqual(error.exception.status, 413)
        self.assertEqual(error.exception.code, "payload_too_large")

    def test_order_request_schema_identity(self):
        create = next(
            route
            for route in ROUTES
            if route["method"] == "POST" and route["path"] == "/orders"
        )
        update = next(
            route
            for route in ROUTES
            if route["method"] == "PATCH" and route["path"] == "/orders/{id}"
        )
        self.assertIs(create["request_schema"], SCHEMAS["CreateOrder"])
        self.assertIs(update["request_schema"], SCHEMAS["UpdateOrder"])

    def test_schema_routes_are_public_and_validate_raw_json(self):
        list_route = next(route for route in ROUTES if route["path"] == "/schemas")
        get_route = next(
            route for route in ROUTES if route["path"] == "/schemas/{name}"
        )
        validate_route = next(
            route for route in ROUTES if route["path"] == "/schemas/{name}/validate"
        )
        self.assertFalse(list_route["auth_required"])
        self.assertFalse(get_route["auth_required"])
        self.assertFalse(validate_route["auth_required"])
        self.assertFalse(validate_route["json_object_only"])
        self.assertEqual(list_route["handler"]([])[1]["items"], sorted(SCHEMAS))
        self.assertIs(
            get_route["handler"]([], {"name": "CreateOrder"})[1],
            SCHEMAS["CreateOrder"],
        )
        self.assertEqual(
            validate_route["handler"]([], {"name": "CreateOrder"}, None)[1]["valid"],
            False,
        )

    @staticmethod
    def make_dispatcher(path, body, method="POST", content_type="application/json"):
        from agent_qa.server import Handler

        headers = Message()
        headers["Content-Length"] = str(len(body))
        headers["Content-Type"] = content_type
        responses = []
        handler = object.__new__(Handler)
        handler.path = path
        handler.command = method
        handler.headers = headers
        handler.rfile = BytesIO(body)
        handler.request_id = "dispatch-test"
        handler._json = lambda *args: responses.append(args)
        return handler, responses

    def test_dispatch_schema_api_accepts_scalar_and_array_json(self):
        from agent_qa.server import Handler

        for raw in (b"null", b"[]"):
            handler, responses = self.make_dispatcher(
                "/schemas/CreateOrder/validate", raw
            )
            Handler._dispatch(handler)
            self.assertEqual(responses[0][0], 200)
            self.assertFalse(responses[0][1]["valid"])

        handler, _ = self.make_dispatcher("/schemas/DoesNotExist/validate", b"null")
        with self.assertRaises(ApiError) as error:
            Handler._dispatch(handler)
        self.assertEqual(error.exception.status, 404)
        self.assertEqual(error.exception.code, "schema_not_found")

    def test_dispatch_auth_and_json_errors_precede_validation(self):
        from agent_qa.server import Handler

        handler, responses = self.make_dispatcher("/orders", b"not-json")
        with patch("agent_qa.server.authenticate_api_key", return_value=None):
            Handler._dispatch(handler)
        self.assertEqual(responses[0][0], 401)

        handler, _ = self.make_dispatcher("/orders", b"{}")
        del handler.headers["Content-Length"]
        with patch("agent_qa.server.authenticate_api_key", return_value=AUTH_IDENTITY):
            with self.assertRaises(ApiError) as error:
                Handler._dispatch(handler)
        self.assertEqual(error.exception.status, 411)
        self.assertEqual(error.exception.code, "length_required")

        handler, _ = self.make_dispatcher("/orders", b" " * 4097)
        with patch("agent_qa.server.authenticate_api_key", return_value=AUTH_IDENTITY):
            with self.assertRaises(ApiError) as error:
                Handler._dispatch(handler)
        self.assertEqual(error.exception.status, 413)
        self.assertEqual(error.exception.code, "payload_too_large")

        handler, _ = self.make_dispatcher(
            "/orders", b"not-json", content_type="text/plain"
        )
        with patch("agent_qa.server.authenticate_api_key", return_value=AUTH_IDENTITY):
            with self.assertRaises(ApiError) as error:
                Handler._dispatch(handler)
        self.assertEqual(error.exception.status, 415)
        self.assertEqual(error.exception.code, "unsupported_media_type")

        for raw in (b"NaN", b"1e999"):
            handler, _ = self.make_dispatcher("/orders", raw)
            with patch(
                "agent_qa.server.authenticate_api_key", return_value=AUTH_IDENTITY
            ):
                with self.assertRaises(ApiError) as error:
                    Handler._dispatch(handler)
            self.assertEqual(error.exception.status, 400)
            self.assertEqual(error.exception.code, "invalid_json")

    def test_csv_body_reader_checks_charset_size_utf8_and_bom(self):
        from agent_qa.server import Handler

        def read(raw, content_type="text/csv"):
            handler = object.__new__(Handler)
            handler.headers = Message()
            handler.headers["Content-Length"] = str(len(raw))
            handler.headers["Content-Type"] = content_type
            handler.rfile = BytesIO(raw)
            return Handler._read_body(
                handler, consumes=["text/csv"], max_body_bytes=65536
            )

        self.assertEqual(
            read(b"\xef\xbb\xbfsku,name\r\nA,Widget\r\n"),
            "sku,name\r\nA,Widget\r\n",
        )
        self.assertEqual(
            read(b"sku,name\r\n", "text/csv; charset=utf-8"), "sku,name\r\n"
        )
        for content_type in ("text/csv; charset=latin-1", "text/csv; charset=utf8"):
            handler = object.__new__(Handler)
            handler.headers = Message()
            handler.headers["Content-Length"] = "1"
            handler.headers["Content-Type"] = content_type
            handler.rfile = BytesIO(b"x")
            with self.subTest(content_type=content_type):
                with self.assertRaises(ApiError) as error:
                    Handler._read_body(handler, consumes=["text/csv"])
                self.assertEqual(error.exception.status, 415)

        for raw, expected_code in (
            (b"\xff", "invalid_csv"),
            (b"x" * 65537, "payload_too_large"),
        ):
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(ApiError) as error:
                    read(raw)
                self.assertEqual(error.exception.code, expected_code)
                self.assertEqual(
                    error.exception.status,
                    400 if expected_code == "invalid_csv" else 413,
                )

        handler = object.__new__(Handler)
        handler.headers = Message()
        handler.headers["Content-Length"] = "9" * 5000
        handler.headers["Content-Type"] = "text/csv"
        handler.rfile = BytesIO()
        with self.assertRaises(ApiError) as error:
            Handler._read_body(handler, consumes=["text/csv"])
        self.assertEqual(error.exception.status, 400)
        self.assertEqual(error.exception.code, "invalid_csv")
        self.assertEqual(error.exception.details[0]["field"], "body")

    def test_csv_export_negotiates_accept_and_rejects_unknown_filters(self):
        from agent_qa.server import Handler

        read_identity = {**AUTH_IDENTITY, "role": "read"}

        for accept in (
            "application/json",
            "text/csv;q=0, */*;q=1",
        ):
            handler, responses = self.make_dispatcher(
                "/exports/products.csv", b"", method="GET"
            )
            handler.headers["Accept"] = accept
            with patch(
                "agent_qa.server.authenticate_api_key", return_value=read_identity
            ):
                Handler._dispatch(handler)
            self.assertEqual(responses[0][0], 406)
        handler, responses = self.make_dispatcher(
            "/exports/products.csv", b"", method="GET"
        )
        handler.headers["Accept"] = "*/*"
        with (
            patch("agent_qa.routes.PRODUCT_STORE.export_csv", return_value=[]),
            patch("agent_qa.server.authenticate_api_key", return_value=read_identity),
        ):
            Handler._dispatch(handler)
        self.assertEqual(responses[0][0], 200)
        self.assertEqual(
            responses[0][1],
            "id,sku,name,category,price_cents,stock,tags,active,created_at,"
            "updated_at\r\n",
        )
        self.assertEqual(responses[0][2]["Content-Type"], "text/csv; charset=utf-8")
        self.assertEqual(
            responses[0][2]["Content-Disposition"],
            'attachment; filename="products.csv"',
        )
        handler, _ = self.make_dispatcher(
            "/exports/products.csv?unexpected=x", b"", method="GET"
        )
        with (
            patch("agent_qa.server.authenticate_api_key", return_value=read_identity),
            self.assertRaises(ApiError) as error,
        ):
            Handler._dispatch(handler)
        self.assertEqual(error.exception.status, 400)
        self.assertEqual(error.exception.code, "invalid_query")

    def test_csv_import_routes_apply_validate_abort_and_skip(self):
        from agent_qa.orders import OrderStore
        from agent_qa.products import ProductStore
        from agent_qa.server import Handler

        products = ProductStore()
        orders = OrderStore()

        def dispatch(path, body):
            handler, responses = self.make_dispatcher(
                path, body, content_type="text/csv; charset=utf-8"
            )
            with (
                patch(
                    "agent_qa.server.authenticate_api_key",
                    return_value=AUTH_IDENTITY,
                ),
                patch("agent_qa.routes.PRODUCT_STORE", products),
                patch("agent_qa.routes.ORDER_STORE", orders),
            ):
                Handler._handle(handler)
            return responses[0]

        status, body, _ = dispatch("/imports/products", b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_csv")
        self.assertEqual(body["error"]["details"][0]["field"], "header")

        good_row = b"SKU-CSV,Widget,tools,1200\r\n"
        status, report, _ = dispatch(
            "/imports/products?mode=validate",
            b"sku,name,category,price_cents\r\n" + good_row,
        )
        self.assertEqual(status, 200)
        self.assertEqual(report["created"], 0)
        self.assertFalse(report["applied"])
        self.assertEqual(products.export_csv({}), [])

        status, report, _ = dispatch(
            "/imports/products",
            b"sku,name,category,price_cents\r\n"
            + good_row
            + b"SKU-BAD,Widget,tools,nope\r\n",
        )
        self.assertEqual(status, 422)
        self.assertEqual(report["created"], 0)
        self.assertEqual(report["errors"][0]["line"], 3)
        self.assertEqual(products.export_csv({}), [])

        status, report, _ = dispatch(
            "/imports/products?on_error=skip",
            b"sku,name,category,price_cents\r\n"
            + good_row
            + b"SKU-BAD,Widget,tools,nope\r\n",
        )
        self.assertEqual(status, 201)
        self.assertEqual(report["created"], 1)
        self.assertTrue(report["applied"])
        self.assertEqual(len(products.export_csv({})), 1)

        status, report, _ = dispatch(
            "/imports/orders",
            b"customer_id,total_cents\r\ncsv-customer-1,1500\r\n",
        )
        self.assertEqual(status, 201)
        self.assertEqual(report["created"], 1)
        self.assertEqual(len(orders.export_csv({})), 1)

        handler, responses = self.make_dispatcher(
            "/exports/orders.csv?status=new&customer_id=csv-customer-1",
            b"",
            method="GET",
        )
        handler.headers["Accept"] = "text/csv"
        read_identity = {**AUTH_IDENTITY, "role": "read"}
        with (
            patch("agent_qa.routes.ORDER_STORE", orders),
            patch("agent_qa.server.authenticate_api_key", return_value=read_identity),
        ):
            Handler._dispatch(handler)
        self.assertEqual(responses[0][0], 200)
        self.assertTrue(
            responses[0][1].startswith(
                "id,customer_id,total_cents,status,items_count,created_at\r\n"
            )
        )

    def test_order_csv_import_policies_and_structural_rejections(self):
        from agent_qa.orders import OrderStore
        from agent_qa.server import Handler

        def dispatch(path, raw, content_type="text/csv; charset=utf-8"):
            handler, responses = self.make_dispatcher(
                path, raw, content_type=content_type
            )
            store = OrderStore()
            with (
                patch(
                    "agent_qa.server.authenticate_api_key",
                    return_value=AUTH_IDENTITY,
                ),
                patch("agent_qa.routes.ORDER_STORE", store),
            ):
                Handler._handle(handler)
            return responses[0], store

        valid = b"customer-1,1500\r\n"
        invalid = b"customer-2,not-an-integer\r\n"
        mixed = b"customer_id,total_cents\r\n" + valid + invalid

        for policy in ("abort", "skip"):
            (status, report, _), store = dispatch(
                f"/imports/orders?mode=validate&on_error={policy}", mixed
            )
            self.assertEqual(status, 200)
            self.assertEqual(report["created"], 0)
            self.assertEqual(report["failed"], 1)
            self.assertFalse(report["applied"])
            self.assertEqual(store.export_csv({}), [])

        (status, report, _), store = dispatch("/imports/orders", mixed)
        self.assertEqual(status, 422)
        self.assertEqual(report["created"], 0)
        self.assertFalse(report["applied"])
        self.assertEqual(store.export_csv({}), [])

        (status, report, _), store = dispatch("/imports/orders?on_error=skip", mixed)
        self.assertEqual(status, 201)
        self.assertEqual(report["created"], 1)
        self.assertTrue(report["applied"])
        self.assertEqual(len(store.export_csv({})), 1)

        (status, report, _), store = dispatch(
            "/imports/orders?on_error=skip",
            b"customer_id,total_cents\r\ncustomer-2,not-an-integer\r\n",
        )
        self.assertEqual(status, 422)
        self.assertEqual(report["created"], 0)
        self.assertFalse(report["applied"])
        self.assertEqual(store.export_csv({}), [])

        (status, report, _), store = dispatch(
            "/imports/orders",
            b"customer_id,total_cents\r\ncustomer-1,1500\r\n",
        )
        self.assertEqual(status, 201)
        self.assertEqual(report["created"], 1)
        self.assertTrue(report["applied"])
        self.assertEqual(len(store.export_csv({})), 1)

        for path in (
            "/imports/orders?mode=preview",
            "/imports/orders?on_error=continue",
            "/imports/orders?extra=x",
        ):
            (status, body, _), _ = dispatch(
                path, b"customer_id,total_cents\r\ncustomer-1,1500\r\n"
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_query")

        structural_cases = (
            (b"", "header"),
            (b"customer_id,total_cents,extra\r\nc,1,x\r\n", "header"),
            (b"customer_id,customer_id,total_cents\r\nc,c,1\r\n", "header"),
            (b"total_cents\r\n1\r\n", "header"),
            (b"customer_id,total_cents\r\n", "either"),
            (b"customer_id,total_cents\r\nc\r\n", "body"),
            (b'customer_id,total_cents\r\n"customer-1,1500\r\n', "body"),
            (
                b"customer_id,total_cents\r\n" + b"customer-1,1500\r\n" * 201,
                "body",
            ),
        )
        for raw, field in structural_cases:
            with self.subTest(field=field, size=len(raw)):
                (status, body, _), _ = dispatch("/imports/orders", raw)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "invalid_csv")
                actual_field = body["error"]["details"][0]["field"]
                if field == "either":
                    self.assertIn(actual_field, {"header", "body"})
                else:
                    self.assertEqual(actual_field, field)

        (status, body, _), _ = dispatch(
            "/imports/orders",
            b"customer_id,total_cents\r\nc,1500\r\n",
            "text/csv; charset=iso-8859-1",
        )
        self.assertEqual(status, 415)
        self.assertEqual(body["error"]["code"], "unsupported_media_type")

        (status, body, _), _ = dispatch(
            "/imports/orders", b"customer_id,total_cents\r\n\xff\r\n"
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_csv")
        self.assertEqual(body["error"]["details"][0]["field"], "body")

        (status, report, _), store = dispatch(
            "/imports/orders",
            b"\xef\xbb\xbfcustomer_id,total_cents\r\ncsv-customer,1500\r\n",
        )
        self.assertEqual(status, 201)
        self.assertTrue(report["applied"])
        self.assertEqual(len(store.export_csv({})), 1)

        (status, body, _), _ = dispatch(
            "/imports/orders",
            b"customer_id,total_cents\r\n" + b"x" * 70000,
        )
        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "payload_too_large")

    def test_dispatch_validates_before_calling_handler(self):
        from agent_qa.server import Handler

        handler, _ = self.make_dispatcher("/dispatch-test", b"{}")
        route_handler = Mock(side_effect=AssertionError("handler should not run"))
        route = {
            "path": "/dispatch-test",
            "method": "POST",
            "handler": route_handler,
            "body": True,
            "auth_required": False,
            "request_schema": {
                "type": "object",
                "required": ["required"],
                "properties": {"required": {"type": "string"}},
            },
        }
        with patch("agent_qa.server.ROUTES", (route,)):
            with self.assertRaises(ApiError) as error:
                Handler._dispatch(handler)
        self.assertEqual(error.exception.code, "validation_error")
        self.assertEqual(
            error.exception.details,
            [{"field": "required", "message": "Required"}],
        )
        route_handler.assert_not_called()

    def test_dispatch_keeps_order_create_and_update_validation_details(self):
        from agent_qa.server import Handler

        cases = (
            (
                "POST",
                "/orders",
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
                "POST",
                "/orders",
                {"customer_id": "customer"},
                [
                    {
                        "field": "items",
                        "message": "Either items or total_cents is required",
                    }
                ],
            ),
            (
                "POST",
                "/orders",
                {
                    "customer_id": "customer",
                    "items": [{"product_id": 1, "quantity": 1}],
                    "total_cents": 1,
                },
                [
                    {
                        "field": "items",
                        "message": "Cannot be combined with total_cents",
                    }
                ],
            ),
            (
                "POST",
                "/orders",
                {
                    "customer_id": "customer",
                    "items": [{"product_id": 1, "quantity": 1001}],
                },
                [
                    {
                        "field": "items[0].quantity",
                        "message": "Must be between 1 and 1000",
                    }
                ],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": None},
                [
                    {"field": "customer_id", "message": "Must be a string"},
                    {
                        "field": "items",
                        "message": "Either items or total_cents is required",
                    },
                ],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": ""},
                [
                    {
                        "field": "customer_id",
                        "message": "Must contain 1 to 64 characters",
                    },
                    {
                        "field": "items",
                        "message": "Either items or total_cents is required",
                    },
                ],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": "  ", "total_cents": 1},
                [{"field": "customer_id", "message": "Must not be blank"}],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": "x" * 65, "total_cents": 1},
                [
                    {
                        "field": "customer_id",
                        "message": "Must contain 1 to 64 characters",
                    }
                ],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": "customer", "total_cents": True},
                [{"field": "total_cents", "message": "Must be an integer"}],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": "customer", "total_cents": -1},
                [
                    {
                        "field": "total_cents",
                        "message": "Must be between 0 and 100000000",
                    }
                ],
            ),
            (
                "POST",
                "/orders",
                {"customer_id": "customer", "total_cents": 1, "status": "paid"},
                [{"field": "status", "message": "Unknown field"}],
            ),
            (
                "PATCH",
                "/orders/1",
                {},
                [{"field": "body", "message": "At least one field is required"}],
            ),
            (
                "PATCH",
                "/orders/1",
                {"status": False},
                [
                    {
                        "field": "status",
                        "message": "Must be one of: cancelled, new, paid, shipped",
                    }
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
                    }
                ],
            ),
            (
                "PATCH",
                "/orders/1",
                {"total_cents": True},
                [{"field": "total_cents", "message": "Must be an integer"}],
            ),
            (
                "PATCH",
                "/orders/1",
                {"total_cents": -1},
                [
                    {
                        "field": "total_cents",
                        "message": "Must be between 0 and 100000000",
                    }
                ],
            ),
            (
                "PATCH",
                "/orders/1",
                {"total_cents": 1.5},
                [{"field": "total_cents", "message": "Must be an integer"}],
            ),
            (
                "PATCH",
                "/orders/1",
                {"unknown": 1},
                [{"field": "unknown", "message": "Unknown field"}],
            ),
            (
                "PATCH",
                "/orders/1",
                {"status": "bad", "unknown": 1},
                [
                    {
                        "field": "status",
                        "message": "Must be one of: cancelled, new, paid, shipped",
                    },
                    {"field": "unknown", "message": "Unknown field"},
                ],
            ),
        )
        for method, path, payload, expected in cases:
            with self.subTest(method=method, payload=payload):
                raw_body = json.dumps(payload).encode("utf-8")
                handler, _ = self.make_dispatcher(path, raw_body, method=method)
                with patch(
                    "agent_qa.server.authenticate_api_key",
                    return_value=AUTH_IDENTITY,
                ):
                    with self.assertRaises(ApiError) as error:
                        Handler._dispatch(handler)
                self.assertEqual(error.exception.status, 400)
                self.assertEqual(error.exception.code, "validation_error")
                self.assertEqual(error.exception.details, expected)

    def test_server_import_does_not_bind_a_port_or_write_files(self):
        code = """
import builtins
import socket

def fail(*args, **kwargs):
    raise AssertionError('unexpected import side effect')

socket.socket.bind = fail
builtins.open = fail
import agent_qa.server
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_app_entry_point_is_at_most_twenty_lines(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        self.assertLessEqual(len(app_path.read_text(encoding="utf-8").splitlines()), 20)


if __name__ == "__main__":
    unittest.main()
