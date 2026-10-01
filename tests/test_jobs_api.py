"""Subprocess API coverage for the background jobs routes."""

import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
API_KEY = "job-api-test-key"


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class JobsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        environment = {
            "APP_PORT": str(cls.port),
            "AGENT_QA_API_KEY": API_KEY,
            "AGENT_QA_GIT_SHA": "jobs-api-test",
        }
        cls.process = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
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
        cls.tearDownClass()
        raise RuntimeError("jobs API service did not start")

    @classmethod
    def tearDownClass(cls):
        process = getattr(cls, "process", None)
        if process is not None and process.poll() is None:
            process.terminate()
        if process is not None:
            process.communicate(timeout=3)

    @classmethod
    def request(cls, method, path, body=None, *, authenticated=False):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["X-API-Key"] = API_KEY
        request = Request(cls.base + path, data=data, headers=headers, method=method)
        try:
            response = urlopen(request, timeout=7)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            return (
                response.status,
                response.headers,
                json.loads(raw) if raw else None,
            )

    @classmethod
    def wait_for_status(cls, job_id, expected):
        deadline = time.monotonic() + 3
        latest = None
        while time.monotonic() < deadline:
            status, _, latest = cls.request("GET", f"/jobs/{job_id}")
            if status == 200 and latest["status"] == expected:
                return latest
            time.sleep(0.01)
        raise AssertionError(f"job {job_id} did not reach {expected}: {latest}")

    def test_job_flow_filters_terminal_cancel_and_full_queue(self):
        status, _, unauthorized = self.request("POST", "/jobs", {"type": "fail"})
        self.assertEqual(status, 401)
        self.assertEqual(unauthorized["error"]["code"], "unauthorized")

        status, _, invalid = self.request(
            "POST",
            "/jobs",
            {"type": "sleep", "params": {"duration_ms": 5001}},
            authenticated=True,
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["code"], "validation_error")
        self.assertEqual(invalid["error"]["details"][0]["field"], "params.duration_ms")

        status, _, product = self.request(
            "POST",
            "/products",
            {
                "sku": "JOB-STOCK",
                "name": "Job stock",
                "category": "tools",
                "price_cents": 120,
                "stock": 2,
            },
            authenticated=True,
        )
        self.assertEqual(status, 201)
        status, _, order = self.request(
            "POST",
            "/orders",
            {"customer_id": "job-customer", "total_cents": 234},
            authenticated=True,
        )
        self.assertEqual(status, 201)
        status, _, order = self.request(
            "PATCH",
            f"/orders/{order['id']}",
            {"status": "paid"},
            authenticated=True,
        )
        self.assertEqual(status, 200)

        status, headers, sleep_job = self.request(
            "POST",
            "/jobs",
            {"type": "sleep", "params": {"duration_ms": 50}},
            authenticated=True,
        )
        self.assertEqual(status, 202)
        self.assertEqual(headers["Location"], f"/jobs/{sleep_job['id']}")
        status, _, sleep_job = self.request(
            "GET", f"/jobs/{sleep_job['id']}?wait_ms=5000"
        )
        self.assertEqual(status, 200)
        self.assertEqual(sleep_job["status"], "succeeded")
        self.assertEqual(sleep_job["result"], {"slept_ms": 50})

        status, _, summary = self.request(
            "POST", "/jobs", {"type": "orders_summary"}, authenticated=True
        )
        self.assertEqual(status, 202)
        status, _, summary = self.request("GET", f"/jobs/{summary['id']}?wait_ms=5000")
        self.assertEqual(status, 200)
        self.assertEqual(summary["result"]["orders"], 1)
        self.assertEqual(summary["result"]["by_status"]["paid"], 1)
        self.assertEqual(summary["result"]["revenue_cents"], 234)

        status, _, stock = self.request(
            "POST",
            "/jobs",
            {"type": "stock_report", "params": {"threshold": 2}},
            authenticated=True,
        )
        self.assertEqual(status, 202)
        status, _, stock = self.request("GET", f"/jobs/{stock['id']}?wait_ms=5000")
        self.assertEqual(status, 200)
        self.assertEqual(
            stock["result"],
            {
                "threshold": 2,
                "low_stock": [
                    {
                        "product_id": product["id"],
                        "sku": "JOB-STOCK",
                        "stock": 2,
                    }
                ],
            },
        )

        running_one = self.request(
            "POST",
            "/jobs",
            {"type": "sleep", "params": {"duration_ms": 5000}},
            authenticated=True,
        )[2]
        self.wait_for_status(running_one["id"], "running")
        status, _, invalid_wait = self.request(
            "GET", f"/jobs/{running_one['id']}?wait_ms=5001"
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid_wait["error"]["code"], "invalid_query")
        running_two = self.request(
            "POST",
            "/jobs",
            {"type": "sleep", "params": {"duration_ms": 5000}},
            authenticated=True,
        )[2]
        self.wait_for_status(running_two["id"], "running")
        queued_for_cancel = self.request(
            "POST", "/jobs", {"type": "fail"}, authenticated=True
        )[2]
        status, _, cancelled_queued = self.request(
            "POST", f"/jobs/{queued_for_cancel['id']}/cancel", authenticated=True
        )
        self.assertEqual(status, 200)
        self.assertEqual(cancelled_queued["status"], "cancelled")

        status, _, cancelling = self.request(
            "POST", f"/jobs/{running_one['id']}/cancel", authenticated=True
        )
        self.assertEqual(status, 200)
        self.assertEqual(cancelling["status"], "cancelling")
        status, _, cancelled_running = self.request(
            "GET", f"/jobs/{running_one['id']}?wait_ms=5000"
        )
        self.assertEqual(status, 200)
        self.assertEqual(cancelled_running["status"], "cancelled")
        self.assertIsNone(cancelled_running["result"])
        self.request("POST", f"/jobs/{running_two['id']}/cancel", authenticated=True)
        self.wait_for_status(running_two["id"], "cancelled")

        status, _, failed = self.request(
            "POST", "/jobs", {"type": "fail"}, authenticated=True
        )
        self.assertEqual(status, 202)
        status, _, failed = self.request("GET", f"/jobs/{failed['id']}?wait_ms=5000")
        self.assertEqual(status, 200)
        self.assertEqual(failed["status"], "failed")
        status, _, conflict = self.request(
            "POST", f"/jobs/{failed['id']}/cancel", authenticated=True
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "job_not_cancellable")

        status, _, filtered = self.request("GET", "/jobs?status=failed&type=fail")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(filtered["total"], 1)
        self.assertTrue(all(item["status"] == "failed" for item in filtered["items"]))

        blockers = []
        for _ in range(2):
            blockers.append(
                self.request(
                    "POST",
                    "/jobs",
                    {"type": "sleep", "params": {"duration_ms": 5000}},
                    authenticated=True,
                )[2]
            )
        for blocker in blockers:
            self.wait_for_status(blocker["id"], "running")

        for _ in range(30):
            status, headers, body = self.request(
                "POST",
                "/jobs",
                {"type": "sleep", "params": {"duration_ms": 5000}},
                authenticated=True,
            )
            if status == 503:
                break
            self.assertEqual(status, 202)
        self.assertEqual(status, 503)
        self.assertEqual(headers["Retry-After"], "1")
        self.assertEqual(body["error"]["code"], "queue_full")

        status, _, queued_jobs = self.request("GET", "/jobs?status=queued&limit=100")
        self.assertEqual(status, 200)
        for queued_job in queued_jobs["items"]:
            cancel_status, _, cancelling_or_cancelled = self.request(
                "POST",
                f"/jobs/{queued_job['id']}/cancel",
                authenticated=True,
            )
            self.assertEqual(cancel_status, 200)
            self.assertIn(
                cancelling_or_cancelled["status"], {"cancelling", "cancelled"}
            )
        for queued_job in queued_jobs["items"]:
            self.wait_for_status(queued_job["id"], "cancelled")
        for blocker in blockers:
            cancel_status, _, cancelling = self.request(
                "POST", f"/jobs/{blocker['id']}/cancel", authenticated=True
            )
            self.assertEqual(cancel_status, 200)
            self.assertEqual(cancelling["status"], "cancelling")
        for blocker in blockers:
            self.wait_for_status(blocker["id"], "cancelled")


if __name__ == "__main__":
    unittest.main()
