"""Subprocess API tests for metrics and safe JSON access logs."""

import json
import os
import socket
import subprocess
import sys
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class ObservabilityApiTests(unittest.TestCase):
    def test_metrics_and_access_logs(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = {"APP_PORT": str(port), "AGENT_QA_GIT_SHA": "observability-test"}
        process = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        base = f"http://127.0.0.1:{port}"
        completed_requests = 0

        def request(method, path, body=None, headers=None):
            nonlocal completed_requests
            data = json.dumps(body).encode("utf-8") if body is not None else None
            request_headers = {"Content-Type": "application/json"}
            if method in {"POST", "PATCH", "DELETE"}:
                request_headers["X-API-Key"] = "qa-synthetic-key"
            request_headers.update(headers or {})
            req = Request(
                base + path,
                data=data,
                headers=request_headers,
                method=method,
            )
            try:
                response = urlopen(req, timeout=2)
            except HTTPError as error:
                response = error
            with response:
                result = response.status, response.headers, response.read()
            completed_requests += 1
            return result

        output = ""
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    if request("GET", "/ready")[0] == 200:
                        break
                except Exception:
                    time.sleep(0.05)
            else:
                self.fail("service did not start")

            for _ in range(3):
                self.assertEqual(request("PUT", "/ready")[0], 405)

            status, headers, first_metrics = request("GET", "/metrics")
            self.assertEqual(status, 200)
            self.assertEqual(
                headers["Content-Type"],
                "text/plain; version=0.0.4; charset=utf-8",
            )
            first_text = first_metrics.decode("utf-8")
            self.assertTrue(first_text.endswith("\n"))
            for name, metric_type in (
                ("agent_qa_http_requests_total", "counter"),
                ("agent_qa_http_request_duration_seconds", "summary"),
                ("agent_qa_orders", "gauge"),
                ("agent_qa_build_info", "gauge"),
            ):
                self.assertIn(f"# HELP {name} ", first_text)
                self.assertIn(f"# TYPE {name} {metric_type}\n", first_text)
            self.assertIn(
                'agent_qa_http_requests_total{method="PUT",route="/ready",'
                'status="405"} 3',
                first_text,
            )
            self.assertNotIn('route="/metrics"', first_text)
            self.assertIn(
                'agent_qa_build_info{git_sha="observability-test"} 1', first_text
            )

            second_text = request("GET", "/metrics")[2].decode("utf-8")
            self.assertIn(
                'agent_qa_http_requests_total{method="GET",route="/metrics",'
                'status="200"} 1',
                second_text,
            )

            status, _, unknown_body = request(
                "GET",
                "/x/y?secret=query-secret",
                headers={"X-API-Key": "header-secret"},
            )
            self.assertEqual(status, 404)
            self.assertNotIn(b"query-secret", unknown_body)
            unmatched_metrics = request("GET", "/metrics")[2].decode("utf-8")
            self.assertIn(
                'agent_qa_http_requests_total{method="GET",route="unmatched",'
                'status="404"} 1',
                unmatched_metrics,
            )
            self.assertNotIn("/x/y", unmatched_metrics)

            payload = {"customer_id": "body-secret", "total_cents": 2400}
            status, _, order_body = request("POST", "/orders", payload)
            self.assertEqual(status, 201)
            order_id = json.loads(order_body)["id"]
            self.assertEqual(request("GET", f"/orders/{order_id}")[0], 200)
            metrics_with_order = request("GET", "/metrics")[2].decode("utf-8")
            self.assertIn("agent_qa_orders 1", metrics_with_order)
            self.assertIn('route="/orders/{id}"', metrics_with_order)
            self.assertEqual(request("DELETE", f"/orders/{order_id}")[0], 204)
            self.assertIn(
                "agent_qa_orders 0", request("GET", "/metrics")[2].decode("utf-8")
            )
        finally:
            process.terminate()
            output, _ = process.communicate(timeout=3)

        log_lines = output.splitlines()
        self.assertEqual(len(log_lines), completed_requests)
        logs = [json.loads(line) for line in log_lines]
        expected_keys = {
            "ts",
            "method",
            "path",
            "route",
            "status",
            "duration_ms",
            "request_id",
        }
        for entry in logs:
            self.assertEqual(set(entry), expected_keys)
            self.assertTrue(entry["ts"].endswith("Z"))
            self.assertIsInstance(entry["status"], int)
            self.assertIsInstance(entry["duration_ms"], float)
            self.assertTrue(entry["request_id"])
        self.assertTrue(any(entry["path"] == "/ready" for entry in logs))
        self.assertTrue(
            any(
                entry["method"] == "GET"
                and entry["path"] == "/x/y"
                and entry["route"] == "unmatched"
                and entry["status"] == 404
                for entry in logs
            )
        )
        self.assertTrue(
            any(
                entry["path"] == f"/orders/{order_id}"
                and entry["route"] == "/orders/{id}"
                for entry in logs
            )
        )
        self.assertNotIn("secret", output)
        self.assertNotIn("header-secret", output)
        self.assertNotIn("qa-synthetic-key", output)
        self.assertNotIn("body-secret", output)


if __name__ == "__main__":
    unittest.main()
