"""Subprocess API coverage for rate-limit enforcement and admin controls."""

import json
import os
import socket
import subprocess
import sys
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class RateLimitApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        cls.admin_key = "ratelimit-api-test-key"
        env = {
            "APP_PORT": str(cls.port),
            "AGENT_QA_API_KEY": cls.admin_key,
            "AGENT_QA_RATE_BURST": "2",
            "AGENT_QA_RATE_REFILL_PER_SECOND": "0.001",
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
                if cls.request("GET", "/ready")[0] == 200:
                    return
            except (OSError, URLError):
                time.sleep(0.05)
        cls.stop_process()
        raise RuntimeError("rate-limit API service did not start")

    @classmethod
    def tearDownClass(cls):
        cls.stop_process()

    @classmethod
    def stop_process(cls):
        if getattr(cls, "process", None) is not None and cls.process.poll() is None:
            cls.process.terminate()
        if getattr(cls, "process", None) is not None:
            try:
                cls.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                cls.process.kill()
                cls.process.wait(timeout=3)

    @classmethod
    def request(cls, method, path, body=None, headers=None):
        request_headers = dict(headers or {})
        data = None if body is None else json.dumps(body).encode("utf-8")
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        request = Request(
            cls.base + path,
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            if not raw:
                payload = None
            elif "json" in response.headers.get("Content-Type", ""):
                payload = json.loads(raw)
            else:
                payload = raw.decode("utf-8")
            return response.status, response.headers, payload

    def client_request(self, method, path, client_id, *, body=None, key=None):
        headers = {"X-Client-Id": client_id}
        if key is not None:
            headers["X-API-Key"] = key
        if method == "POST":
            headers["Idempotency-Key"] = "rate-limit-retry"
        return self.request(method, path, body, headers)

    def admin_request(self, method, path, body=None):
        return self.request(
            method,
            path,
            body,
            {"X-API-Key": self.admin_key},
        )

    def reset_identity(self, identity):
        put = self.admin_request(
            "PUT",
            f"/admin/rate-limits/{identity}",
            {"burst": 2, "refill_per_second": 0.001},
        )
        self.assertEqual(put[0], 200)
        deleted = self.admin_request("DELETE", f"/admin/rate-limits/{identity}")
        self.assertEqual(deleted[0], 204)

    def setUp(self):
        self.reset_identity("key:bootstrap")
        self.reset_identity("ip:127.0.0.1")

    def tearDown(self):
        self.reset_identity("key:bootstrap")
        self.reset_identity("ip:127.0.0.1")

    def test_headers_limit_exemptions_and_rate_limited_envelope(self):
        for _ in range(5):
            status, headers, _ = self.request("GET", "/ready")
            self.assertEqual(status, 200)
            self.assertNotIn("RateLimit-Limit", headers)

        first = self.client_request("GET", "/orders", "headers-check")
        second = self.client_request("GET", "/orders", "headers-check")
        self.assertEqual(first[0], 200)
        self.assertEqual(first[1]["RateLimit-Limit"], "2")
        self.assertEqual(first[1]["RateLimit-Remaining"], "1")
        self.assertEqual(first[1]["RateLimit-Reset"], "1000")
        self.assertEqual(second[0], 200)

        denied = self.client_request("GET", "/orders", "headers-check")
        self.assertEqual(denied[0], 429)
        self.assertEqual(denied[2]["error"]["code"], "rate_limited")
        self.assertEqual(denied[2]["error"]["message"], "Rate limit exceeded")
        self.assertEqual(denied[1]["Retry-After"], "1000")
        self.assertEqual(denied[1]["RateLimit-Remaining"], "0")
        metrics = self.request("GET", "/metrics")[2]
        self.assertIn('agent_qa_rate_limited_total{kind="client"}', metrics)
        self.assertNotIn("headers-check", metrics)

        missing = self.request("GET", "/missing-route")
        wrong_method = self.request("PUT", "/ready")
        self.assertEqual(missing[0], 404)
        self.assertEqual(wrong_method[0], 405)
        self.assertNotIn("RateLimit-Limit", missing[1])
        self.assertNotIn("RateLimit-Limit", wrong_method[1])

    def test_key_client_and_ip_identity_precedence_and_invalid_key_consumption(self):
        key_id = "priority-client"
        self.assertEqual(
            self.client_request("GET", "/orders", key_id, key=self.admin_key)[0], 200
        )
        # Two distinct clients still share the valid API key's bucket.
        self.assertEqual(
            self.client_request("GET", "/orders", "second-client", key=self.admin_key)[
                0
            ],
            200,
        )
        # The valid key wins over a changed client ID and remains exhausted.
        denied = self.client_request(
            "GET", "/orders", "different-client", key=self.admin_key
        )
        self.assertEqual(denied[0], 429)

        client_id = "invalid-key-bucket"
        self.assertEqual(self.client_request("GET", "/orders", client_id)[0], 200)
        self.assertEqual(
            self.client_request("GET", "/orders", client_id, key="invalid-key")[0],
            200,
        )
        self.assertEqual(self.client_request("GET", "/orders", client_id)[0], 429)

        # Distinct valid client IDs do not share the loopback IP bucket.
        self.assertEqual(self.client_request("GET", "/orders", "ip-isolated-a")[0], 200)
        self.assertEqual(self.client_request("GET", "/orders", "ip-isolated-b")[0], 200)

        # Invalid client IDs fall back to the shared source IP bucket.
        for invalid_id in ("invalid/id-one", "invalid/id-two"):
            self.assertEqual(self.client_request("GET", "/orders", invalid_id)[0], 200)
        self.assertEqual(
            self.client_request("GET", "/orders", "invalid/id-three")[0], 429
        )

    def test_authentication_errors_include_rate_limit_headers(self):
        first = self.client_request(
            "GET", "/audit", "auth-error-headers", key="invalid-auth-key"
        )
        self.assertEqual(first[0], 401)
        self.assertEqual(first[1]["RateLimit-Limit"], "2")
        self.assertEqual(first[1]["RateLimit-Remaining"], "1")

    def test_admin_override_reset_and_denied_idempotency_does_not_reserve_key(self):
        identity = "key:bootstrap"
        for _ in range(2):
            self.assertEqual(
                self.client_request(
                    "GET", "/orders", "idempotency-client", key=self.admin_key
                )[0],
                200,
            )
        denied = self.client_request(
            "POST",
            "/orders",
            "idempotency-client",
            body={"customer_id": "rate-limit-customer", "total_cents": 1250},
            key=self.admin_key,
        )
        self.assertEqual(denied[0], 429)

        put = self.admin_request(
            "PUT",
            f"/admin/rate-limits/{identity}",
            {"burst": 3, "refill_per_second": 0.5},
        )
        self.assertEqual(put[0], 200)
        self.assertEqual(put[2]["identity"], identity)
        listing = self.admin_request("GET", "/admin/rate-limits")
        self.assertEqual(listing[0], 200)
        self.assertEqual(listing[2]["overrides"][identity]["burst"], 3)

        success = self.client_request(
            "POST",
            "/orders",
            "idempotency-client",
            body={"customer_id": "rate-limit-customer", "total_cents": 1250},
            key=self.admin_key,
        )
        self.assertEqual(success[0], 201)

        deleted = self.admin_request("DELETE", f"/admin/rate-limits/{identity}")
        self.assertEqual(deleted[0], 204)
        missing = self.admin_request("DELETE", f"/admin/rate-limits/{identity}")
        self.assertEqual(missing[0], 404)
        self.assertEqual(missing[2]["error"]["code"], "override_not_found")


if __name__ == "__main__":
    unittest.main()
