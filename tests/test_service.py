import io
import json
import os
import platform
import subprocess
import sys
import time
import unittest
import socket
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

import app


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

    def test_ready_accepts_query_string(self):
        with urlopen(self.base + "/ready?x=1", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(
                json.load(response), {"status": "ready", "git_sha": "test-sha-123"}
            )

    def test_version_returns_service_details(self):
        with urlopen(self.base + "/version", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(
                response.headers["Content-Type"], "application/json; charset=utf-8"
            )
            self.assertEqual(
                json.load(response),
                {
                    "service": "agent-qa",
                    "git_sha": "test-sha-123",
                    "python_version": platform.python_version(),
                },
            )

    def test_version_accepts_query_string(self):
        with urlopen(self.base + "/version?a=b", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response)["service"], "agent-qa")

    def test_version_trailing_slash_is_not_found(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base + "/version/", timeout=2)
        self.assertEqual(error.exception.code, 404)

    def test_fixture_is_synthetic(self):
        with urlopen(self.base + "/fixture", timeout=2) as response:
            fixture = json.load(response)
        self.assertEqual(fixture["record_type"], "synthetic_customer_fixture")
        self.assertEqual(fixture["email"], "qa-customer-0001@example.invalid")
        self.assertFalse(fixture["is_real_person"])

    def test_fixture_matches_synthetic_customer_file(self):
        fixture_path = (
            Path(__file__).resolve().parents[1] / "data" / "synthetic-customer.json"
        )
        with fixture_path.open(encoding="utf-8") as fixture_file:
            expected_fixture = json.load(fixture_file)

        with urlopen(self.base + "/fixture", timeout=2) as response:
            self.assertEqual(response.status, 200)
            fixture = json.load(response)

        self.assertEqual(fixture, expected_fixture)
        self.assertEqual(fixture["record_type"], "synthetic_customer_fixture")

    def test_unknown_route_is_not_found(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base + "/missing", timeout=2)
        self.assertEqual(error.exception.code, 404)
        self.assertEqual(
            json.load(error.exception),
            {"error": {"code": "not_found", "message": "Route not found"}},
        )

    def test_unsupported_methods_return_json_errors(self):
        for method, path in (
            ("POST", "/ready"),
            ("PUT", "/fixture"),
            ("PATCH", "/version"),
            ("DELETE", "/ready"),
        ):
            with self.subTest(method=method, path=path):
                request = Request(self.base + path, method=method)
                with self.assertRaises(HTTPError) as error:
                    urlopen(request, timeout=2)
                self.assertEqual(error.exception.code, 405)
                self.assertEqual(error.exception.headers["Allow"], "GET")
                self.assertEqual(
                    json.load(error.exception),
                    {
                        "error": {
                            "code": "method_not_allowed",
                            "message": "Method not allowed",
                        }
                    },
                )

    def test_unsupported_method_on_unknown_route_is_not_found(self):
        request = Request(self.base + "/nope", method="POST")
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=2)
        self.assertEqual(error.exception.code, 404)
        self.assertEqual(
            json.load(error.exception),
            {"error": {"code": "not_found", "message": "Route not found"}},
        )

    def test_unexpected_get_exception_returns_safe_json_500(self):
        handler = object.__new__(app.Handler)
        handler.path = "/fixture"
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()

        with patch.object(Path, "open", side_effect=RuntimeError("private details")):
            handler.do_GET()

        handler.send_response.assert_called_once_with(500)
        body = handler.wfile.getvalue().decode("utf-8")
        self.assertEqual(
            json.loads(body),
            {
                "error": {
                    "code": "internal_error",
                    "message": "Internal server error",
                }
            },
        )
        self.assertNotIn("Traceback", body)
        self.assertNotIn("private details", body)


if __name__ == "__main__":
    unittest.main()
