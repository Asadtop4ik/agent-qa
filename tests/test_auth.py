"""API-key authentication tests for write routes."""

import json
import os
import socket
import subprocess
import sys
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_qa.auth import api_key_from_headers, is_valid_api_key
from agent_qa.config import API_KEY


ROOT = os.path.dirname(os.path.dirname(__file__))
DEFAULT_KEY = "qa-synthetic-key"


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class TestServer:
    def __init__(self, api_key=None, set_api_key=False, capture_output=False):
        self.port = free_port()
        env = {"APP_PORT": str(self.port), "AGENT_QA_GIT_SHA": "auth-test"}
        if set_api_key:
            env["AGENT_QA_API_KEY"] = api_key
        self.process = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=capture_output,
        )
        self.base = f"http://127.0.0.1:{self.port}"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                self.request("GET", "/ready")
                return
            except Exception:
                time.sleep(0.05)
        self.stop()
        raise RuntimeError("service did not start")

    def request(self, method, path, body=None, headers=None, raw_body=None):
        data = raw_body
        if data is None and body is not None:
            data = json.dumps(body).encode("utf-8")
        request = Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
            method=method,
        )
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            content = response.read()
            try:
                parsed = json.loads(content) if content else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = content
            return response.status, response.headers, parsed

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
        output, _ = self.process.communicate(timeout=3)
        return output or ""


class AuthUnitTests(unittest.TestCase):
    def test_header_parsing(self):
        self.assertIsNone(api_key_from_headers({}))
        self.assertEqual(api_key_from_headers({"X-API-Key": "key"}), "key")
        self.assertEqual(api_key_from_headers({"x-api-key": "lowercase"}), "lowercase")
        self.assertEqual(api_key_from_headers({"X-API-Key": ""}), "")

    def test_key_comparison(self):
        self.assertTrue(is_valid_api_key({"X-API-Key": API_KEY}))
        for value in (None, "", "wrong", "é"):
            headers = {} if value is None else {"X-API-Key": value}
            with self.subTest(value=value):
                self.assertFalse(is_valid_api_key(headers))


class AuthApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = TestServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def assert_unauthorized(self, result):
        status, headers, body = result
        self.assertEqual(status, 401)
        self.assertEqual(headers["WWW-Authenticate"], "X-API-Key")
        self.assertEqual(body["error"]["code"], "unauthorized")
        self.assertEqual(body["error"]["message"], "Invalid or missing API key")
        self.assertEqual(body["error"]["request_id"], headers["X-Request-Id"])

    def test_missing_empty_wrong_and_non_ascii_keys_are_rejected(self):
        for headers in (
            {},
            {"X-API-Key": ""},
            {"X-API-Key": "wrong"},
            {"X-API-Key": "é"},
        ):
            with self.subTest(headers=headers):
                self.assert_unauthorized(
                    self.server.request(
                        "POST",
                        "/orders",
                        {"customer_id": "auth-test", "total_cents": 100},
                        headers=headers,
                    )
                )

    def test_correct_key_allows_create_patch_and_delete(self):
        headers = {"X-API-Key": DEFAULT_KEY}
        status, _, created = self.server.request(
            "POST",
            "/orders",
            {"customer_id": "auth-write", "total_cents": 100},
            headers=headers,
        )
        self.assertEqual(status, 201)
        path = f"/orders/{created['id']}"
        status, _, updated = self.server.request(
            "PATCH", path, {"status": "paid"}, headers=headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["status"], "paid")
        self.assertEqual(self.server.request("DELETE", path, headers=headers)[0], 204)

    def test_read_routes_remain_open(self):
        status, _, created = self.server.request(
            "POST",
            "/orders",
            {"customer_id": "auth-read", "total_cents": 100},
            headers={"X-API-Key": DEFAULT_KEY},
        )
        self.assertEqual(status, 201)
        for path in (
            "/orders",
            f"/orders/{created['id']}",
            "/ready",
            "/fixture",
            "/version",
            "/metrics",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.server.request("GET", path)[0], 200)

    def test_route_method_body_and_resource_checks_follow_authentication(self):
        self.assert_unauthorized(self.server.request("DELETE", "/orders/999999"))
        self.assertEqual(
            self.server.request(
                "DELETE", "/orders/999999", headers={"X-API-Key": DEFAULT_KEY}
            )[0],
            404,
        )
        self.assertEqual(self.server.request("POST", "/unknown")[0], 404)
        self.assertEqual(self.server.request("PUT", "/orders")[0], 405)
        self.assert_unauthorized(
            self.server.request("POST", "/orders", raw_body=b" " * 5000)
        )

    def test_unauthorized_writes_do_not_change_store(self):
        total = self.server.request("GET", "/orders")[2]["total"]
        self.assert_unauthorized(
            self.server.request(
                "POST",
                "/orders",
                {"customer_id": "must-not-exist", "total_cents": 100},
            )
        )
        self.assertEqual(self.server.request("GET", "/orders")[2]["total"], total)

        status, _, created = self.server.request(
            "POST",
            "/orders",
            {"customer_id": "patch-no-auth", "total_cents": 100},
            headers={"X-API-Key": DEFAULT_KEY},
        )
        self.assertEqual(status, 201)
        path = f"/orders/{created['id']}"
        self.assert_unauthorized(self.server.request("PATCH", path, {"status": "paid"}))
        self.assertEqual(self.server.request("GET", path)[2]["status"], "new")
        self.assert_unauthorized(self.server.request("DELETE", path))
        self.assertEqual(self.server.request("GET", path)[0], 200)

    def test_unauthorized_requests_are_counted_without_exposing_key(self):
        marker = "sensitive-auth-marker"
        server = TestServer(capture_output=True)
        try:
            response = server.request(
                "POST",
                "/orders",
                {"customer_id": "auth-metric", "total_cents": 1},
                headers={"X-API-Key": marker},
            )
            self.assert_unauthorized(response)
            self.assertNotIn(marker, json.dumps(response[2]))
            serialized_headers = json.dumps(dict(response[1]))
            self.assertNotIn(marker, serialized_headers)
            self.assertNotIn(DEFAULT_KEY, serialized_headers)
            metrics = server.request("GET", "/metrics")[2]
        finally:
            logs = server.stop()
        self.assertIn(
            'agent_qa_http_requests_total{method="POST",route="/orders",'
            'status="401"}',
            metrics,
        )
        self.assertNotIn(marker, metrics)
        self.assertNotIn(DEFAULT_KEY, metrics)
        self.assertNotIn(marker, logs)
        self.assertNotIn(DEFAULT_KEY, logs)


class AuthEnvironmentTests(unittest.TestCase):
    def assert_server_key(self, api_key, set_api_key, valid_key, invalid_key):
        server = TestServer(api_key=api_key, set_api_key=set_api_key)
        try:
            payload = {"customer_id": "env-auth", "total_cents": 1}
            self.assertEqual(
                server.request(
                    "POST", "/orders", payload, headers={"X-API-Key": invalid_key}
                )[0],
                401,
            )
            self.assertEqual(
                server.request(
                    "POST", "/orders", payload, headers={"X-API-Key": valid_key}
                )[0],
                201,
            )
        finally:
            server.stop()

    def test_custom_and_empty_environment_values(self):
        self.assert_server_key("custom-test-key", True, "custom-test-key", DEFAULT_KEY)
        self.assert_server_key("", True, DEFAULT_KEY, "custom-test-key")


if __name__ == "__main__":
    unittest.main()
