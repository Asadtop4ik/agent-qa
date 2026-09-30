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


if __name__ == "__main__":
    unittest.main()
