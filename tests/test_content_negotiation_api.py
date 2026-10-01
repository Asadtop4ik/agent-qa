"""Subprocess tests for HTTP representation negotiation."""

import gzip
import json
import os
import socket
import subprocess
import sys
import time
import unittest
from email.message import Message
from io import BytesIO
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_qa.conditional import parse_etag_list, weak_match
from agent_qa.errors import ApiError, envelope
from agent_qa.server import Handler


class ContentNegotiationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        env = {
            "APP_PORT": str(cls.port),
            "AGENT_QA_GIT_SHA": "content-negotiation-test",
            "AGENT_QA_API_KEY": "qa-synthetic-key",
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
            except OSError:
                time.sleep(0.05)
        cls.process.terminate()
        raise RuntimeError("service did not start")

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=3)

    def request(self, method, path, headers=None, body=None):
        request = Request(
            self.base + path,
            data=body,
            method=method,
            headers=headers or {},
        )
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def test_problem_and_legacy_errors(self):
        status, headers, body = self.request(
            "GET",
            "/missing?ignored=yes",
            {"Accept": "application/problem+json;q=0.8, application/json;q=0.7"},
        )
        self.assertEqual(status, 404)
        self.assertEqual(
            headers["Content-Type"], "application/problem+json; charset=utf-8"
        )
        self.assertIn("Accept", headers["Vary"])
        self.assertEqual(
            json.loads(body),
            {
                "type": "https://agent-qa.invalid/problems/not_found",
                "title": "Not Found",
                "status": 404,
                "detail": "Route not found",
                "instance": "/missing",
                "code": "not_found",
                "request_id": headers["X-Request-Id"],
            },
        )

        for accept in (None, "*/*"):
            request_headers = {"Accept": accept} if accept is not None else {}
            status, headers, body = self.request("GET", "/missing", request_headers)
            self.assertEqual(status, 404)
            self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
            self.assertIn("Accept", headers["Vary"])
            self.assertIn("error", json.loads(body))

    def test_mismatched_accept_prevents_order_creation(self):
        _, _, before = self.request("GET", "/orders")
        payload = json.dumps(
            {"customer_id": "negotiation-test", "total_cents": 1500}
        ).encode()
        status, headers, body = self.request(
            "POST",
            "/orders",
            {
                "Accept": "text/plain",
                "Content-Type": "application/json",
                "X-API-Key": "qa-synthetic-key",
            },
            payload,
        )
        self.assertEqual(status, 406)
        self.assertEqual(json.loads(body)["error"]["code"], "not_acceptable")
        self.assertIn("Accept", headers["Vary"])
        _, _, after = self.request("GET", "/orders")
        self.assertEqual(json.loads(after), json.loads(before))

    def test_content_encoding_is_checked_after_auth_before_body(self):
        headers = {
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        }
        status, _, body = self.request("POST", "/orders", headers, b"not-read")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "unauthorized")

        headers["X-API-Key"] = "qa-synthetic-key"
        status, _, body = self.request("POST", "/orders", headers, b"not-read")
        self.assertEqual(status, 415)
        self.assertEqual(
            json.loads(body)["error"]["code"], "unsupported_content_encoding"
        )

    def test_gzip_is_deterministic_and_small_ready_response_stays_plain(self):
        fixed_id = {"X-Request-Id": "negotiation-gzip-test"}
        _, _, plain_body = self.request("GET", "/openapi.json", fixed_id)
        self.assertGreaterEqual(len(plain_body), 256)
        headers = fixed_id | {"Accept-Encoding": "gzip"}
        _, compressed_headers, first = self.request("GET", "/openapi.json", headers)
        _, repeated_headers, second = self.request("GET", "/openapi.json", headers)
        self.assertEqual(compressed_headers["Content-Encoding"], "gzip")
        self.assertEqual(compressed_headers["Vary"], "Accept-Encoding")
        self.assertEqual(compressed_headers["Content-Length"], str(len(first)))
        self.assertEqual(first, second)
        self.assertEqual(gzip.decompress(first), plain_body)
        self.assertEqual(first, gzip.compress(plain_body, mtime=0))
        self.assertEqual(repeated_headers["Content-Length"], str(len(second)))

        _, ready_headers, ready_body = self.request("GET", "/ready", headers)
        self.assertNotIn("Content-Encoding", ready_headers)
        self.assertLess(len(ready_body), 256)

        _, ping_headers, _ = self.request("GET", "/ping", headers)
        self.assertNotIn("Content-Encoding", ping_headers)


class NegotiationHandlerTests(unittest.TestCase):
    @staticmethod
    def make_handler(method="GET", path="/ready", headers=None, body=b""):
        handler = object.__new__(Handler)
        handler.command = method
        handler.path = path
        handler.headers = headers or Message()
        handler.rfile = BytesIO(body)
        handler.wfile = BytesIO()
        handler.request_id = "handler-negotiation-test"
        handler.sent_statuses = []
        handler.sent_headers = []
        handler._record_response = lambda status: None
        handler.send_response = handler.sent_statuses.append
        handler.send_header = lambda name, value: handler.sent_headers.append(
            (name, value)
        )
        handler.end_headers = lambda: None
        return handler

    @staticmethod
    def response_header(handler, name):
        return next(
            value
            for header, value in handler.sent_headers
            if header.lower() == name.lower()
        )

    def test_problem_q_preference_preserves_headers_and_merges_vary(self):
        headers = Message()
        headers["Accept"] = "application/problem+json;q=0.8, application/json;q=0.8"
        headers["Accept-Encoding"] = "gzip"
        handler = self.make_handler("GET", "/failure?secret=query", headers)
        details = [{"field": "x", "message": "e" * 400}]
        Handler._json(
            handler,
            409,
            envelope("conflict", "Conflict", details, request_id=handler.request_id),
            {"Retry-After": "4"},
        )
        self.assertEqual(handler.sent_statuses, [409])
        self.assertEqual(
            self.response_header(handler, "Content-Type"),
            "application/problem+json; charset=utf-8",
        )
        self.assertEqual(self.response_header(handler, "Retry-After"), "4")
        self.assertEqual(
            self.response_header(handler, "Vary"),
            "Accept, Accept-Encoding",
        )
        encoded = handler.wfile.getvalue()
        expected_problem = {
            "type": "https://agent-qa.invalid/problems/conflict",
            "title": "Conflict",
            "status": 409,
            "detail": "Conflict",
            "instance": "/failure",
            "code": "conflict",
            "request_id": handler.request_id,
            "errors": details,
        }
        self.assertEqual(
            gzip.decompress(encoded),
            json.dumps(expected_problem, separators=(",", ":")).encode(),
        )

    def test_problem_q0_or_lower_preference_keeps_legacy_envelope(self):
        for accept in (
            "application/problem+json;q=0, */*;q=1",
            "application/problem+json;q=0.4, application/json;q=0.5",
        ):
            with self.subTest(accept=accept):
                headers = Message()
                headers["Accept"] = accept
                handler = self.make_handler(headers=headers)
                Handler._json(
                    handler,
                    404,
                    envelope("not_found", "Route not found", request_id="id"),
                )
                self.assertEqual(
                    self.response_header(handler, "Content-Type"),
                    "application/json; charset=utf-8",
                )
                self.assertIn("error", json.loads(handler.wfile.getvalue()))

    def test_accept_mismatch_prevents_handler_and_content_encoding_reads_no_body(self):
        route_handler = Mock(side_effect=AssertionError("route must not run"))
        route = {
            "method": "POST",
            "path": "/negotiation-test",
            "handler": route_handler,
            "produces": ["application/json"],
        }
        headers = Message()
        headers["Accept"] = "text/plain"
        headers["Content-Length"] = "5"
        handler = self.make_handler("POST", "/negotiation-test", headers, b"body!")
        with patch("agent_qa.server.ROUTES", [route]):
            Handler._handle(handler)
        route_handler.assert_not_called()
        self.assertEqual(handler.sent_statuses, [406])
        self.assertEqual(
            json.loads(handler.wfile.getvalue())["error"]["code"], "not_acceptable"
        )

        headers = Message()
        headers["Content-Length"] = "8"
        headers["Content-Encoding"] = "gzip"
        handler = self.make_handler("POST", "/orders", headers)
        handler.rfile = Mock()
        handler.rfile.read.side_effect = AssertionError("body must not be read")
        with patch("agent_qa.server.authenticate_api_key", return_value=None):
            Handler._dispatch(handler)
        self.assertEqual(handler.sent_statuses, [401])
        handler = self.make_handler("POST", "/orders", headers)
        handler.rfile = Mock()
        handler.rfile.read.side_effect = AssertionError("body must not be read")
        with patch(
            "agent_qa.server.authenticate_api_key",
            return_value={"key_id": "test", "role": "write"},
        ):
            with self.assertRaises(ApiError) as error:
                Handler._dispatch(handler)
        self.assertEqual(error.exception.status, 415)
        self.assertEqual(error.exception.code, "unsupported_content_encoding")

    def test_problem_json_is_accepted_by_json_routes_and_404_405_precede_406(self):
        route_handler = Mock(return_value=(200, {"ok": True}, {}))
        route = {
            "method": "GET",
            "path": "/json-route",
            "handler": route_handler,
            "produces": ["application/json"],
        }
        headers = Message()
        headers["Accept"] = "application/problem+json"
        handler = self.make_handler("GET", "/json-route", headers)
        with patch("agent_qa.server.ROUTES", [route]):
            Handler._dispatch(handler)
        route_handler.assert_called_once()
        self.assertEqual(
            self.response_header(handler, "Content-Type"),
            "application/json; charset=utf-8",
        )

        route_handler.reset_mock()
        headers = Message()
        headers["Accept"] = "text/plain"
        handler = self.make_handler("POST", "/json-route", headers)
        with patch("agent_qa.server.ROUTES", [route]):
            Handler._dispatch(handler)
        self.assertEqual(handler.sent_statuses, [405])
        self.assertEqual(self.response_header(handler, "Allow"), "GET")
        self.assertEqual(
            json.loads(handler.wfile.getvalue())["error"]["code"],
            "method_not_allowed",
        )
        handler = self.make_handler("GET", "/missing", headers)
        with patch("agent_qa.server.ROUTES", [route]):
            Handler._dispatch(handler)
        self.assertEqual(handler.sent_statuses, [404])

    def test_small_body_still_varies_on_accept_encoding_without_compression(self):
        headers = Message()
        headers["Accept-Encoding"] = "gzip"
        handler = self.make_handler(path="/small", headers=headers)
        body = {"ok": True}
        Handler._json(handler, 200, body)
        self.assertEqual(self.response_header(handler, "Vary"), "Accept-Encoding")
        self.assertNotIn("Content-Encoding", [name for name, _ in handler.sent_headers])
        self.assertEqual(
            handler.wfile.getvalue(), json.dumps(body, separators=(",", ":")).encode()
        )

    def test_compressed_etag_is_weak_but_source_headers_remain_canonical(self):
        headers = Message()
        headers["Accept-Encoding"] = "gzip"
        handler = self.make_handler(path="/resource", headers=headers)
        canonical = '"resource.1"'
        route_headers = {"ETag": canonical}
        body = {"data": "x" * 300}
        Handler._json(handler, 200, body, route_headers)
        self.assertEqual(self.response_header(handler, "ETag"), f"W/{canonical}")
        self.assertEqual(route_headers["ETag"], canonical)
        serialized = json.dumps(body, separators=(",", ":")).encode()
        self.assertEqual(gzip.decompress(handler.wfile.getvalue()), serialized)

        conditional_handler = self.make_handler(
            "GET",
            "/orders",
            Message(),
        )
        conditional_handler.headers["Accept-Encoding"] = "gzip"
        conditional_handler.headers["If-None-Match"] = f"W/{canonical}"

        def conditional_response(query, params, payload, request_headers):
            parsed = parse_etag_list(request_headers["If-None-Match"])
            if weak_match(parsed, canonical):
                return 304, None, {"ETag": canonical}
            return 200, body, {"ETag": canonical}

        conditional_route = {
            "method": "GET",
            "path": "/orders",
            "conditional_headers": ["If-None-Match"],
            "handler": conditional_response,
        }
        with patch("agent_qa.server.ROUTES", [conditional_route]):
            Handler._dispatch(conditional_handler)
        self.assertEqual(conditional_handler.sent_statuses, [304])
        self.assertEqual(self.response_header(conditional_handler, "ETag"), canonical)
