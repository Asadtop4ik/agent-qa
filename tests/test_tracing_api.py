"""Subprocess HTTP coverage for request tracing and the admin trace API."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
ADMIN_KEY = "trace-test-admin-key"


class TracingApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        env = {
            "PATH": os.environ.get("PATH", ""),
            "APP_PORT": str(cls.port),
            "AGENT_QA_GIT_SHA": "tracing-api-test",
            "AGENT_QA_API_KEY": ADMIN_KEY,
            "AGENT_QA_TRACE_CAPACITY": "10",
        }
        cls.output = tempfile.TemporaryFile()
        cls.process = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=ROOT,
            env=env,
            stdout=cls.output,
            stderr=subprocess.DEVNULL,
        )
        cls.base = f"http://127.0.0.1:{cls.port}"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                cls.request("GET", "/ready")
                return
            except Exception:
                time.sleep(0.05)
        cls.process.terminate()
        cls.process.wait(timeout=3)
        raise RuntimeError("service did not start")

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=3)
        cls.output.close()

    @classmethod
    def request(cls, method, path, headers=None, body=None):
        request_headers = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = Request(
            cls.base + path,
            data=data,
            method=method,
            headers=request_headers,
        )
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def test_propagation_admin_lookup_and_sensitive_input_exclusion(self):
        known_trace = "abcdef0123456789abcdef0123456789"
        incoming = f"00-{known_trace}-0123456789abcdef-01"
        status, headers, _ = self.request(
            "GET",
            "/health?private_marker=do-not-retain",
            {"traceparent": incoming},
        )
        self.assertEqual(status, 200)
        self.assertRegex(
            headers["traceparent"],
            rf"^00-{known_trace}-[0-9a-f]{{16}}-01$",
        )
        self.assertRegex(
            headers["Server-Timing"], r"http\.request;dur=[0-9]+\.[0-9]{3}"
        )
        trace_id = headers["traceparent"].split("-")[1]

        status, admin_headers, body = self.request(
            "GET", "/admin/traces?limit=10", {"X-API-Key": ADMIN_KEY}
        )
        self.assertEqual(status, 200)
        traces = json.loads(body)
        self.assertGreaterEqual(traces["total_matching"], 1)
        self.assertEqual(traces["items"][0]["trace_id"], trace_id)
        self.assertNotIn(b"do-not-retain", body)
        self.assertIsNotNone(admin_headers.get("traceparent"))

        status, _, body = self.request(
            "GET", f"/admin/traces/{trace_id}", {"X-API-Key": ADMIN_KEY}
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["spans"][0]["name"], "http.request")

        status, order_headers, _ = self.request(
            "GET", "/orders", {"X-API-Key": ADMIN_KEY}
        )
        self.assertEqual(status, 200)
        order_trace_id = order_headers["traceparent"].split("-")[1]
        status, _, body = self.request(
            "GET",
            f"/admin/traces/{order_trace_id}",
            {"X-API-Key": ADMIN_KEY},
        )
        self.assertEqual(status, 200)
        spans = json.loads(body)["spans"]
        names = {item["name"] for item in spans}
        self.assertTrue(
            {"http.request", "route_match", "auth", "handler", "store"} <= names
        )
        store_span = next(item for item in spans if item["name"] == "store")
        self.assertEqual(store_span["attrs"]["op"], "list")

    def test_invalid_queries_missing_trace_and_admin_auth_have_trace_headers(self):
        admin = {"X-API-Key": ADMIN_KEY}
        for query in (
            "limit=0",
            "limit=1&limit=2",
            "status=200.1",
            "min_duration_ms=NaN",
            "min_duration_ms=-1",
            "unknown=x",
        ):
            with self.subTest(query=query):
                status, headers, body = self.request(
                    "GET", f"/admin/traces?{query}", admin
                )
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"]["code"], "invalid_query")
                self.assertIn("traceparent", headers)
                self.assertIn("Server-Timing", headers)

        status, headers, body = self.request("GET", "/admin/traces/not-a-trace", admin)
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "trace_not_found")
        self.assertIn("traceparent", headers)
        self.assertIn("Server-Timing", headers)

        status, headers, _ = self.request("GET", "/admin/traces")
        self.assertEqual(status, 401)
        self.assertIn("traceparent", headers)
        self.assertIn("Server-Timing", headers)

        status, _, body = self.request(
            "POST",
            "/admin/keys",
            {"X-API-Key": ADMIN_KEY},
            {"role": "read", "label": "trace-reader"},
        )
        self.assertEqual(status, 201)
        read_key = json.loads(body)["key"]
        status, headers, body = self.request(
            "GET", "/admin/traces", {"X-API-Key": read_key}
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "forbidden")
        self.assertIn("traceparent", headers)
        self.assertIn("Server-Timing", headers)

    def test_trace_filters_accept_unbounded_integers_and_nonnegative_durations(self):
        admin = {"X-API-Key": ADMIN_KEY}
        for query, expect_empty in (
            ("status=-1", True),
            ("status=0", True),
            ("status=600", True),
            ("status=1000", True),
            ("min_duration_ms=0", False),
            ("min_duration_ms=1000000000", True),
            ("min_duration_ms=1000000001", True),
        ):
            with self.subTest(query=query):
                status, _, body = self.request("GET", f"/admin/traces?{query}", admin)
                self.assertEqual(status, 200)
                result = json.loads(body)
                self.assertEqual(len(result["items"]), result["total_matching"])
                if expect_empty:
                    self.assertEqual(result["items"], [])

    def test_invalid_parent_starts_trace_and_not_modified_response_has_headers(self):
        status, headers, _ = self.request("GET", "/health", {"traceparent": "bad"})
        self.assertEqual(status, 200)
        self.assertRegex(headers["traceparent"], r"^00-[0-9a-f]{32}-[0-9a-f]{16}-01$")

        status, first_headers, _ = self.request(
            "GET", "/orders", {"X-API-Key": ADMIN_KEY}
        )
        self.assertEqual(status, 200)
        status, headers, body = self.request(
            "GET",
            "/orders",
            {"X-API-Key": ADMIN_KEY, "If-None-Match": first_headers["ETag"]},
        )
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.assertIn("traceparent", headers)
        self.assertIn("Server-Timing", headers)

    def test_metrics_and_access_log_include_completed_span_and_trace_id(self):
        known_trace = "1234567890abcdef1234567890abcdef"
        status, headers, _ = self.request(
            "GET",
            "/health?private_marker=log-secret",
            {"traceparent": f"00-{known_trace}-0123456789abcdef-01"},
        )
        self.assertEqual(status, 200)
        trace_id = headers["traceparent"].split("-")[1]

        self.request("GET", "/metrics")
        status, _, body = self.request("GET", "/metrics")
        metrics = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn(
            'agent_qa_span_duration_seconds_count{span="http.request"}', metrics
        )

        self.output.flush()
        self.output.seek(0)
        lines = self.output.read().decode("utf-8").splitlines()
        entries = [json.loads(line) for line in lines if line.startswith("{")]
        self.assertTrue(any(entry.get("trace_id") == trace_id for entry in entries))
        self.assertNotIn("log-secret", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
