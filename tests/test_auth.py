"""Authentication unit and subprocess API tests."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_qa import auth
from agent_qa.routes import ROUTES


class ApiKeyUnitTests(unittest.TestCase):
    def test_route_auth_flags(self):
        self.assertTrue(
            all(isinstance(route["auth_required"], bool) for route in ROUTES)
        )
        self.assertEqual(
            {
                (route["method"], route["path"])
                for route in ROUTES
                if route["auth_required"]
            },
            {
                ("POST", "/orders"),
                ("PATCH", "/orders/{id}"),
                ("DELETE", "/orders/{id}"),
            },
        )

    def test_header_api_key_parsing(self):
        self.assertIsNone(auth.header_api_key(None))
        self.assertIsNone(auth.header_api_key(""))
        self.assertEqual(auth.header_api_key(" key "), " key ")

    def test_key_comparison_uses_utf8_values_and_rejects_missing_values(self):
        with patch.object(auth, "API_KEY", "clé-secrète"):
            self.assertTrue(auth.is_valid_api_key("clé-secrète"))
            self.assertFalse(auth.is_valid_api_key("cle-secrete"))
            self.assertFalse(auth.is_valid_api_key(None))
            self.assertFalse(auth.is_valid_api_key(""))


class AuthApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.process, cls.port, cls.log_file = cls.start_server()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.stop_server(cls.process, cls.log_file)

    @staticmethod
    def start_server(api_key_marker=...):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = {"APP_PORT": str(port), "AGENT_QA_GIT_SHA": "auth-test"}
        if api_key_marker is not ...:
            env["AGENT_QA_API_KEY"] = api_key_marker
        log_file = tempfile.TemporaryFile()
        process = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=env,
            stdout=log_file,
            stderr=log_file,
        )
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                urlopen(base + "/ready", timeout=0.2).close()
                return process, port, log_file
            except Exception:
                time.sleep(0.05)
        AuthApiTests.stop_server(process, log_file)
        raise RuntimeError("service did not start")

    @staticmethod
    def stop_server(process, log_file):
        process.terminate()
        process.wait(timeout=3)
        log_file.close()

    def request(self, method, path, payload=None, headers=None, raw_body=None):
        request_headers = {"Content-Type": "application/json"}
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
            try:
                parsed_body = json.loads(body) if body else None
            except json.JSONDecodeError:
                parsed_body = body.decode("utf-8")
            return (
                response.status,
                response.headers,
                parsed_body,
            )

    def authorized(self, method, path, payload=None):
        return self.request(
            method,
            path,
            payload,
            headers={"X-API-Key": "qa-synthetic-key"},
        )

    def test_missing_wrong_empty_and_non_ascii_keys_are_unauthorized(self):
        before = self.request("GET", "/orders")[2]["total"]
        keys = (None, "wrong-test-key", "", "clé-test-key")
        for key in keys:
            headers = {} if key is None else {"X-API-Key": key}
            with self.subTest(key=key):
                status, response_headers, body = self.request(
                    "POST",
                    "/orders",
                    {"customer_id": "must-not-create", "total_cents": 100},
                    headers=headers,
                )
                self.assertEqual(status, 401)
                self.assertEqual(response_headers["WWW-Authenticate"], "X-API-Key")
                self.assertEqual(body["error"]["code"], "unauthorized")
                self.assertEqual(body["error"]["message"], "Invalid or missing API key")
                for possible_key in ("wrong-test-key", "clé-test-key"):
                    self.assertNotIn(possible_key, json.dumps(body))
        after = self.request("GET", "/orders")[2]["total"]
        self.assertEqual(after, before)

    def test_valid_key_permits_create_patch_and_delete(self):
        status, _, created = self.authorized(
            "POST",
            "/orders",
            {"customer_id": "auth-write-test", "total_cents": 500},
        )
        self.assertEqual(status, 201)
        order_path = f"/orders/{created['id']}"
        status, _, updated = self.authorized("PATCH", order_path, {"status": "paid"})
        self.assertEqual(status, 200)
        self.assertEqual(updated["status"], "paid")
        status, _, body = self.authorized("DELETE", order_path)
        self.assertEqual(status, 204)
        self.assertIsNone(body)

    def test_read_endpoints_are_public(self):
        status, _, order = self.authorized(
            "POST",
            "/orders",
            {"customer_id": "auth-public-read", "total_cents": 100},
        )
        self.assertEqual(status, 201)
        for path in (
            "/orders",
            f"/orders/{order['id']}",
            "/ready",
            "/fixture",
            "/version",
            "/metrics",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 200)

    def test_route_and_method_checks_precede_authentication(self):
        self.assertEqual(self.request("POST", "/unknown")[0], 404)
        status, _, method_error = self.request("PUT", "/orders")
        self.assertEqual(status, 405)
        self.assertEqual(method_error["error"]["code"], "method_not_allowed")
        status, _, error = self.request("DELETE", "/orders/999999")
        self.assertEqual(status, 401)
        self.assertEqual(error["error"]["code"], "unauthorized")
        status, _, error = self.authorized("DELETE", "/orders/999999")
        self.assertEqual(status, 404)
        self.assertEqual(error["error"]["code"], "order_not_found")
        status, _, error = self.request("POST", "/orders", raw_body=b" " * 5000)
        self.assertEqual(status, 401)
        self.assertEqual(error["error"]["code"], "unauthorized")

    def test_unauthorized_patch_does_not_change_order_or_leak_key(self):
        status, _, order = self.authorized(
            "POST",
            "/orders",
            {"customer_id": "auth-unchanged", "total_cents": 123},
        )
        self.assertEqual(status, 201)
        path = f"/orders/{order['id']}"
        unauthorized_key = "must-not-appear-test-key"
        status, _, error = self.request(
            "PATCH", path, {"status": "paid"}, headers={"X-API-Key": unauthorized_key}
        )
        self.assertEqual(status, 401)
        self.assertEqual(error["error"]["code"], "unauthorized")
        self.assertEqual(self.request("GET", path)[2]["status"], "new")

        metrics_text = self.request("GET", "/metrics")[2]
        self.assertIn('status="401"', metrics_text)
        self.assertNotIn(unauthorized_key, metrics_text)
        self.assertNotIn(unauthorized_key, json.dumps(error))
        self.log_file.flush()
        self.log_file.seek(0)
        self.assertNotIn(unauthorized_key.encode(), self.log_file.read())

    def test_custom_and_empty_environment_keys(self):
        for configured, accepted, rejected in (
            ("custom-test-key", "custom-test-key", "qa-synthetic-key"),
            ("", "qa-synthetic-key", "custom-test-key"),
        ):
            process, port, log_file = self.start_server(configured)
            try:
                base = f"http://127.0.0.1:{port}"
                for key, expected_status in ((rejected, 401), (accepted, 201)):
                    request = Request(
                        base + "/orders",
                        data=json.dumps(
                            {
                                "customer_id": "env-test-" + key,
                                "total_cents": 200,
                            }
                        ).encode("utf-8"),
                        headers={
                            "Content-Type": "application/json",
                            "X-API-Key": key,
                        },
                        method="POST",
                    )
                    try:
                        response = urlopen(request, timeout=2)
                    except HTTPError as error:
                        response = error
                    with response:
                        self.assertEqual(response.status, expected_status)
            finally:
                self.stop_server(process, log_file)


if __name__ == "__main__":
    unittest.main()
