"""Subprocess API tests for the in-memory orders resource."""

import http.client
import json
import os
import socket
import subprocess
import sys
import time
import unittest
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class OrdersApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        env = {
            "APP_PORT": str(cls.port),
            "AGENT_QA_GIT_SHA": "orders-api-test",
        }
        cls.process = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.base = f"http://127.0.0.1:{cls.port}"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                urlopen(cls.base + "/ready", timeout=0.2).close()
                return
            except Exception:
                time.sleep(0.05)
        cls.process.terminate()
        raise RuntimeError("service did not start")

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=3)

    def customer_id(self):
        return "api-test-" + uuid.uuid4().hex

    def request(
        self, method, path, payload=None, headers=None, raw_body=None, public=False
    ):
        request_headers = {"Content-Type": "application/json"}
        if method in {"POST", "PATCH", "DELETE"} and not public:
            request_headers["X-API-Key"] = "qa-synthetic-key"
        request_headers.update(headers or {})
        if raw_body is not None:
            data = raw_body
        elif payload is not None:
            data = json.dumps(payload).encode("utf-8")
        else:
            data = None
        request = Request(
            self.base + path,
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            body = response.read()
            return (
                response.status,
                response.headers,
                json.loads(body) if body else None,
            )

    def request_without_content_length(self, method, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.putrequest(method, path)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("X-API-Key", "qa-synthetic-key")
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
        result = (
            response.status,
            response.headers,
            json.loads(body) if body else None,
        )
        connection.close()
        return result

    def create_order(self, customer_id=None, total_cents=1500):
        customer_id = customer_id or self.customer_id()
        status, headers, body = self.request(
            "POST",
            "/orders",
            {"customer_id": customer_id, "total_cents": total_cents},
        )
        self.assertEqual(status, 201)
        self.assertEqual(headers["Location"], f"/orders/{body['id']}")
        self.assertEqual(body["customer_id"], customer_id)
        self.assertEqual(body["status"], "new")
        self.assertTrue(body["created_at"].endswith("Z"))
        self.assertEqual(body["items"], [])
        return body

    def create_product(self, *, stock=10, active=True, price_cents=123, name="Widget"):
        sku = "Q" + uuid.uuid4().hex[:10].upper()
        status, _, body = self.request(
            "POST",
            "/products",
            {
                "sku": sku,
                "name": name,
                "category": "tools",
                "price_cents": price_cents,
                "stock": stock,
                "active": active,
            },
        )
        self.assertEqual(status, 201)
        return body

    def assert_error(self, response, status, code):
        actual_status, headers, body = response
        self.assertEqual(actual_status, status)
        self.assertEqual(body["error"]["code"], code)
        self.assertEqual(body["error"]["request_id"], headers["X-Request-Id"])
        return body["error"]

    def test_create_get_filter_and_pagination(self):
        customer = self.customer_id()
        first = self.create_order(customer)
        second = self.create_order(customer, 2500)
        other = self.create_order(self.customer_id())

        status, _, body = self.request("GET", f"/orders?customer_id={customer}")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 2)
        self.assertEqual(
            [item["id"] for item in body["items"]], [first["id"], second["id"]]
        )

        status, _, body = self.request(
            "GET", f"/orders?customer_id={customer}&status=new&limit=1&offset=1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["limit"], 1)
        self.assertEqual(body["offset"], 1)
        self.assertEqual(body["items"], [second])
        self.assertNotEqual(other["customer_id"], customer)

    def test_query_validation(self):
        for query in (
            "limit=0",
            "limit=101",
            "limit=abc",
            "offset=-1",
            "status=foo",
            "x=1",
            "limit=1&limit=2",
        ):
            with self.subTest(query=query):
                self.assert_error(
                    self.request("GET", "/orders?" + query), 400, "invalid_query"
                )

    def test_post_validation_and_body_errors(self):
        customer = self.customer_id()
        invalid_payloads = (
            {},
            {"customer_id": customer, "total_cents": True},
            {"customer_id": customer, "total_cents": -1},
            {"customer_id": "x" * 65, "total_cents": 1},
            {"customer_id": customer, "total_cents": 1, "status": "paid"},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                error = self.assert_error(
                    self.request("POST", "/orders", payload),
                    400,
                    "validation_error",
                )
                self.assertEqual(
                    [item["field"] for item in error["details"]],
                    sorted(item["field"] for item in error["details"]),
                )

        missing_choice = self.assert_error(
            self.request("POST", "/orders", {"customer_id": customer}),
            400,
            "validation_error",
        )
        self.assertEqual(
            missing_choice["details"],
            [{"field": "items", "message": "Either items or total_cents is required"}],
        )
        combined_choice = self.assert_error(
            self.request(
                "POST",
                "/orders",
                {
                    "customer_id": customer,
                    "items": [{"product_id": 1, "quantity": 1}],
                    "total_cents": 1,
                },
            ),
            400,
            "validation_error",
        )
        self.assertEqual(
            combined_choice["details"],
            [{"field": "items", "message": "Cannot be combined with total_cents"}],
        )

        status, _, body = self.request("POST", "/orders", raw_body=b"not json")
        self.assert_error((status, _, body), 400, "invalid_json")
        self.assert_error(
            self.request("POST", "/orders", raw_body=b"[]"), 400, "invalid_json"
        )
        self.assert_error(
            self.request(
                "POST",
                "/orders",
                raw_body=b"{}",
                headers={"Content-Type": "text/plain"},
            ),
            415,
            "unsupported_media_type",
        )
        self.assert_error(
            self.request("POST", "/orders", raw_body=b" " * 5000),
            413,
            "payload_too_large",
        )
        self.assert_error(
            self.request_without_content_length("POST", "/orders"),
            411,
            "length_required",
        )

    def test_schema_api_and_order_validation_compatibility(self):
        status, _, body = self.request("GET", "/schemas", public=True)
        self.assertEqual(status, 200)
        self.assertEqual(body["items"], sorted(body["items"]))
        self.assertIn("CreateOrder", body["items"])

        status, _, schema = self.request("GET", "/schemas/CreateOrder", public=True)
        self.assertEqual(status, 200)
        self.assertEqual(schema["type"], "object")
        self.assert_error(
            self.request("GET", "/schemas/unknown", public=True),
            404,
            "schema_not_found",
        )
        status, _, result = self.request(
            "POST",
            "/schemas/CreateOrder/validate",
            {"customer_id": "schema-check", "total_cents": 10},
            public=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result, {"valid": True, "errors": []})
        status, _, result = self.request(
            "POST",
            "/schemas/CreateOrder/validate",
            raw_body=b"null",
            public=True,
        )
        self.assertEqual(status, 200)
        self.assertFalse(result["valid"])

    def test_patch_transitions_and_locked_price(self):
        order = self.create_order()
        path = f"/orders/{order['id']}"
        status, _, paid = self.request("PATCH", path, {"status": "paid"})
        self.assertEqual(status, 200)
        self.assertEqual(paid["status"], "paid")
        self.assert_error(
            self.request("PATCH", path, {"status": "new"}), 409, "invalid_transition"
        )
        self.assert_error(
            self.request("PATCH", path, {"total_cents": 2000}), 409, "order_locked"
        )
        fresh = self.create_order()
        self.assert_error(
            self.request("PATCH", f"/orders/{fresh['id']}", {"status": "shipped"}),
            409,
            "invalid_transition",
        )
        self.assert_error(
            self.request("PATCH", f"/orders/{fresh['id']}", {}),
            400,
            "validation_error",
        )

    def test_item_order_errors_and_snapshot_response(self):
        customer = self.customer_id()
        product = self.create_product(name="Before", price_cents=123, stock=10)
        payload = {
            "customer_id": customer,
            "items": [{"product_id": product["id"], "quantity": 2}],
        }
        status, _, order = self.request("POST", "/orders", payload)
        self.assertEqual(status, 201)
        self.assertEqual(order["total_cents"], 246)
        self.assertEqual(
            order["items"],
            [
                {
                    "product_id": product["id"],
                    "sku": product["sku"],
                    "name": "Before",
                    "quantity": 2,
                    "unit_price_cents": 123,
                    "line_total_cents": 246,
                }
            ],
        )
        status, _, updated_product = self.request(
            "PATCH",
            f"/products/{product['id']}",
            {"name": "After", "price_cents": 999},
        )
        self.assertEqual(status, 200)
        status, _, fetched = self.request("GET", f"/orders/{order['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["items"][0]["name"], "Before")
        self.assertEqual(fetched["items"][0]["unit_price_cents"], 123)
        self.assertEqual(updated_product["stock"], 8)
        computed_total = self.assert_error(
            self.request("PATCH", f"/orders/{order['id']}", {"total_cents": 10}),
            409,
            "total_computed",
        )
        self.assertEqual(computed_total["code"], "total_computed")
        status, _, cancelled = self.request(
            "PATCH", f"/orders/{order['id']}", {"status": "cancelled"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(cancelled["status"], "cancelled")
        status, _, restored = self.request("GET", f"/products/{product['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(restored["stock"], 10)
        status, _, body = self.request("DELETE", f"/orders/{order['id']}")
        self.assertEqual(status, 204)
        self.assertIsNone(body)
        status, _, restored_again = self.request("GET", f"/products/{product['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(restored_again["stock"], 10)

        duplicate = self.assert_error(
            self.request(
                "POST",
                "/orders",
                {
                    "customer_id": self.customer_id(),
                    "items": [
                        {"product_id": product["id"], "quantity": 1},
                        {"product_id": product["id"], "quantity": 1},
                    ],
                },
            ),
            400,
            "validation_error",
        )
        self.assertEqual(
            duplicate["details"],
            [{"field": "items[1].product_id", "message": "Duplicate product"}],
        )
        unknown = self.assert_error(
            self.request(
                "POST",
                "/orders",
                {
                    "customer_id": self.customer_id(),
                    "items": [{"product_id": 999999, "quantity": 1}],
                },
            ),
            400,
            "validation_error",
        )
        self.assertEqual(
            unknown["details"],
            [{"field": "items[0].product_id", "message": "Unknown product"}],
        )
        inactive = self.create_product(active=False)
        unavailable = self.assert_error(
            self.request(
                "POST",
                "/orders",
                {
                    "customer_id": self.customer_id(),
                    "items": [{"product_id": inactive["id"], "quantity": 1}],
                },
            ),
            409,
            "product_unavailable",
        )
        self.assertEqual(
            unavailable["details"],
            [{"field": "items[0].product_id", "message": "Product is unavailable"}],
        )

    def test_insufficient_stock_details_keep_numeric_item_order(self):
        products = [self.create_product(stock=0) for _ in range(11)]
        response = self.request(
            "POST",
            "/orders",
            {
                "customer_id": self.customer_id(),
                "items": [
                    {"product_id": product["id"], "quantity": 1} for product in products
                ],
            },
        )
        error = self.assert_error(response, 409, "insufficient_stock")
        self.assertEqual(
            error["details"],
            [
                {"field": f"items[{index}].quantity", "message": "Only 0 in stock"}
                for index in range(11)
            ],
        )

    def test_get_missing_and_delete_id_is_not_reused(self):
        self.assert_error(self.request("GET", "/orders/abc"), 404, "order_not_found")
        self.assert_error(
            self.request("GET", "/orders/999999999"), 404, "order_not_found"
        )
        order = self.create_order()
        path = f"/orders/{order['id']}"
        status, headers, body = self.request("DELETE", path)
        self.assertEqual(status, 204)
        self.assertEqual(headers["Content-Length"], "0")
        self.assertEqual(body, None)
        self.assert_error(self.request("DELETE", path), 404, "order_not_found")
        next_order = self.create_order()
        self.assertGreater(next_order["id"], order["id"])

    def test_method_allow_lists_are_sorted(self):
        for path, expected in (
            ("/orders", "GET, POST"),
            ("/orders/123", "DELETE, GET, PATCH"),
        ):
            with self.subTest(path=path):
                response = self.request("PUT", path)
                self.assert_error(response, 405, "method_not_allowed")
                self.assertEqual(response[1]["Allow"], expected)

    def test_conditional_order_reads_and_writes(self):
        order = self.create_order()
        path = f"/orders/{order['id']}"
        status, headers, fetched = self.request("GET", path)
        self.assertEqual(status, 200)
        initial_etag = headers["ETag"]
        self.assertNotIn("version", fetched)

        status, headers, body = self.request(
            "GET", path, headers={"If-None-Match": f"W/{initial_etag}"}
        )
        self.assertEqual(status, 304)
        self.assertIsNone(body)
        self.assertEqual(headers["ETag"], initial_etag)
        self.assertNotIn("Content-Type", headers)

        stale = self.request(
            "PATCH", path, {"status": "paid"}, headers={"If-Match": '"o1.0"'}
        )
        self.assertEqual(stale[0], 412)
        self.assertEqual(stale[1]["ETag"], initial_etag)
        self.assertEqual(stale[2]["error"]["code"], "precondition_failed")
        invalid = self.request(
            "PATCH", path, {"status": "paid"}, headers={"If-Match": "o1.1"}
        )
        self.assert_error(invalid, 400, "invalid_precondition")

        status, headers, paid = self.request(
            "PATCH", path, {"status": "paid"}, headers={"If-Match": headers["ETag"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["ETag"], initial_etag.rsplit(".", 1)[0] + '.2"')
        self.assertEqual(paid["status"], "paid")

    def test_conditional_lists_and_product_stock(self):
        status, headers, _ = self.request("GET", "/orders")
        self.assertEqual(status, 200)
        self.assertTrue(headers["ETag"].startswith('W/"'))
        status, _, body = self.request(
            "GET",
            "/orders",
            headers={"If-None-Match": f'"unrelated", {headers["ETag"]}'},
        )
        self.assertEqual(status, 304)
        self.assertIsNone(body)

        product = self.create_product(stock=4)
        path = f"/products/{product['id']}"
        status, headers, current = self.request("GET", path)
        self.assertEqual(status, 200)
        initial_etag = headers["ETag"]
        self.assertNotIn("version", current)
        status, headers, changed = self.request(
            "POST",
            path + "/adjust-stock",
            {"delta": 1},
            headers={"If-Match": initial_etag},
        )
        self.assertEqual(status, 200)
        self.assertEqual(changed["stock"], 5)
        self.assertEqual(headers["ETag"], initial_etag.rsplit(".", 1)[0] + '.2"')

    def test_item_order_stock_changes_product_etag_and_replay_keeps_create_etag(self):
        product = self.create_product(stock=3)
        path = f"/products/{product['id']}"
        status, headers, _ = self.request("GET", path)
        initial_product_etag = headers["ETag"]

        order_payload = {
            "customer_id": self.customer_id(),
            "items": [{"product_id": product["id"], "quantity": 1}],
        }
        status, order_headers, order = self.request(
            "POST",
            "/orders",
            order_payload,
            headers={"Idempotency-Key": "conditional-order-replay"},
        )
        self.assertEqual(status, 201)
        first_etag = order_headers["ETag"]
        replay = self.request(
            "POST",
            "/orders",
            order_payload,
            headers={"Idempotency-Key": "conditional-order-replay"},
        )
        self.assertEqual(replay[0], 201)
        self.assertEqual(replay[1]["ETag"], first_etag)
        self.assertEqual(replay[1]["Idempotent-Replay"], "true")

        status, product_headers, reserved = self.request("GET", path)
        self.assertEqual(reserved["stock"], 2)
        self.assertEqual(
            product_headers["ETag"], initial_product_etag.rsplit(".", 1)[0] + '.2"'
        )
        reserved_etag = product_headers["ETag"]
        changed = self.request(
            "PATCH", f"/orders/{order['id']}", {"status": "cancelled"}
        )
        self.assertEqual(changed[0], 200)
        self.assertNotEqual(changed[1]["ETag"], first_etag)
        replay = self.request(
            "POST",
            "/orders",
            order_payload,
            headers={"Idempotency-Key": "conditional-order-replay"},
        )
        self.assertEqual(replay[1]["ETag"], first_etag)
        self.assertEqual(replay[2], order)
        status, product_headers, released = self.request("GET", path)
        self.assertEqual(released["stock"], 3)
        self.assertEqual(
            product_headers["ETag"], reserved_etag.rsplit(".", 1)[0] + '.3"'
        )
        released_etag = product_headers["ETag"]

        second_payload = {
            "customer_id": self.customer_id(),
            "items": [{"product_id": product["id"], "quantity": 1}],
        }
        status, _, second_order = self.request("POST", "/orders", second_payload)
        self.assertEqual(status, 201)
        fourth_etag = self.request("GET", path)[1]["ETag"]
        self.assertEqual(fourth_etag, released_etag.rsplit(".", 1)[0] + '.4"')
        status, _, body = self.request("DELETE", f"/orders/{second_order['id']}")
        self.assertEqual(status, 204)
        self.assertIsNone(body)
        self.assertEqual(
            self.request("GET", path)[1]["ETag"], fourth_etag.rsplit(".", 1)[0] + '.5"'
        )

    def test_conditional_header_missing_can_be_required_in_subprocess(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = {
            "APP_PORT": str(port),
            "AGENT_QA_GIT_SHA": "if-match-required-test",
            "AGENT_QA_REQUIRE_IF_MATCH": "true",
        }
        process = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 5
            while True:
                try:
                    urlopen(base + "/ready", timeout=0.2).close()
                    break
                except Exception:
                    if time.monotonic() >= deadline:
                        self.fail("required-header service did not start")
                    time.sleep(0.05)
            product_body = {
                "sku": "REQ-1",
                "name": "Required",
                "category": "tools",
                "price_cents": 1,
            }
            request = Request(
                base + "/products",
                data=json.dumps(product_body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": "qa-synthetic-key",
                },
                method="POST",
            )
            created = urlopen(request, timeout=2)
            product = json.loads(created.read())
            created.close()
            request = Request(
                base + f"/products/{product['id']}",
                data=b'{"name":"Changed"}',
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": "qa-synthetic-key",
                },
                method="PATCH",
            )
            with self.assertRaises(HTTPError) as response:
                urlopen(request, timeout=2)
            self.assertEqual(response.exception.code, 428)
            self.assertEqual(
                json.loads(response.exception.read())["error"]["code"],
                "precondition_required",
            )
        finally:
            process.terminate()
            process.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
