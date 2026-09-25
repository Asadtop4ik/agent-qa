import json
import os
import subprocess
import sys
import time
import unittest
import socket
from urllib.error import HTTPError
from urllib.request import urlopen


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        env = os.environ | {
            "AGENT_QA_GIT_SHA": "test-sha-123",
            "APP_PORT": str(cls.port),
        }
        cls.process = subprocess.Popen(
            [sys.executable, "app.py"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.base = f"http://127.0.0.1:{cls.port}"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                urlopen(cls.base + "/ready", timeout=0.2)
                return
            except Exception:
                time.sleep(0.05)
        cls.process.terminate()
        raise RuntimeError("service did not start")

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=3)

    def test_ready_returns_built_sha(self):
        with urlopen(self.base + "/ready", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(
                json.load(response), {"status": "ready", "git_sha": "test-sha-123"}
            )

    def test_fixture_is_synthetic(self):
        with urlopen(self.base + "/fixture", timeout=2) as response:
            fixture = json.load(response)
        self.assertEqual(fixture["record_type"], "synthetic_customer_fixture")
        self.assertEqual(fixture["email"], "qa-customer-0001@example.invalid")
        self.assertFalse(fixture["is_real_person"])

    def test_unknown_route_is_not_found(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base + "/missing", timeout=2)
        self.assertEqual(error.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
