"""Subprocess API coverage for HTTP representation negotiation."""

import gzip
import json
import os
import socket
import subprocess
import sys
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ContentNegotiationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        cls.process = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env={"APP_PORT": str(cls.port)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.base = f"http://127.0.0.1:{cls.port}"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                urlopen(cls.base + "/ready", timeout=0.2).close()
                return
            except (URLError, TimeoutError, ConnectionError):
                time.sleep(0.05)
        cls.process.terminate()
        cls.process.wait(timeout=3)
        raise RuntimeError("service did not start")

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=3)

    def request(self, method, path, payload=None, headers=None):
        request_headers = {}
        data = None
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        request_headers.update(headers or {})
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
            return response.status, response.headers, response.read()

    def test_problem_and_legacy_error_formats(self):
        status, headers, body = self.request(
            "GET",
            "/fixture?fields=not_a_field",
            headers={"Accept": "application/problem+json, application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            headers["Content-Type"], "application/problem+json; charset=utf-8"
        )
        problem = json.loads(body)
        self.assertEqual(
            problem["type"], "https://agent-qa.invalid/problems/invalid_query"
        )
        self.assertEqual(problem["title"], "Bad Request")
        self.assertEqual(problem["status"], 400)
        self.assertEqual(problem["instance"], "/fixture")
        self.assertTrue(problem["errors"])
        self.assertIn("Accept", headers["Vary"])

        status, headers, body = self.request("GET", "/missing")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertIn("Accept", headers["Vary"])

        status, _, body = self.request("GET", "/missing", headers={"Accept": "*/*"})
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")

    def test_406_does_not_run_post_handler(self):
        customer = "negotiation-no-side-effect"
        status, _, body = self.request(
            "POST",
            "/orders",
            {"customer_id": customer, "total_cents": 25},
            {
                "X-API-Key": "qa-synthetic-key",
                "Accept": "image/jpeg",
            },
        )
        self.assertEqual(status, 406)
        self.assertEqual(json.loads(body)["error"]["code"], "not_acceptable")
        status, _, body = self.request(
            "GET",
            f"/orders?customer_id={customer}",
            headers={"X-API-Key": "qa-synthetic-key"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["total"], 0)

    def test_mismatched_accept_does_not_replace_404_or_405(self):
        status, _, _ = self.request("GET", "/missing", headers={"Accept": "image/jpeg"})
        self.assertEqual(status, 404)
        status, headers, _ = self.request(
            "PUT", "/ready", headers={"Accept": "image/jpeg"}
        )
        self.assertEqual(status, 405)
        self.assertIn("GET", headers["Allow"])

    def test_gzip_is_deterministic_and_ready_stays_uncompressed(self):
        first_status, first_headers, first_body = self.request(
            "GET", "/openapi.json", headers={"Accept-Encoding": "gzip"}
        )
        second_status, _, second_body = self.request(
            "GET", "/openapi.json", headers={"Accept-Encoding": "gzip"}
        )
        self.assertEqual(first_status, second_status)
        self.assertEqual(first_headers["Content-Encoding"], "gzip")
        self.assertEqual(first_headers["Content-Length"], str(len(first_body)))
        self.assertEqual(first_body, second_body)
        status, _, plain_body = self.request(
            "GET", "/openapi.json", headers={"Accept-Encoding": "identity"}
        )
        self.assertEqual(status, first_status)
        self.assertEqual(gzip.decompress(first_body), plain_body)
        self.assertEqual(json.loads(plain_body)["openapi"], "3.0.3")
        self.assertIn("Accept-Encoding", first_headers["Vary"])

        status, headers, ready_body = self.request(
            "GET", "/ready", headers={"Accept-Encoding": "gzip"}
        )
        self.assertEqual(status, 200)
        self.assertNotIn("Content-Encoding", headers)
        self.assertEqual(json.loads(ready_body)["status"], "ready")

        status, headers, ping_body = self.request(
            "GET", "/ping", headers={"Accept-Encoding": "gzip"}
        )
        self.assertEqual(status, 200)
        self.assertNotIn("Content-Encoding", headers)
        self.assertEqual(json.loads(ping_body), {"pong": True})

    def test_unsupported_request_content_encoding_is_rejected(self):
        status, _, body = self.request(
            "POST",
            "/orders",
            {"customer_id": "compressed-input", "total_cents": 1},
            {
                "X-API-Key": "qa-synthetic-key",
                "Content-Encoding": "gzip",
            },
        )
        self.assertEqual(status, 415)
        self.assertEqual(
            json.loads(body)["error"]["code"], "unsupported_content_encoding"
        )


if __name__ == "__main__":
    unittest.main()
