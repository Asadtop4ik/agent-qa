"""Socket-free dispatch and subprocess HTTP API tests for rate limiting."""

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

from agent_qa.audit import AuditLog
from agent_qa.context import RequestContext, clear_context, set_context
from agent_qa.errors import envelope
from agent_qa.idempotency import IdempotencyStore
from agent_qa.ratelimit import TokenBucketLimiter
from agent_qa.server import Handler


class RateLimitDispatchTests(unittest.TestCase):
    def setUp(self):
        self.now = 50.0
        self.limiter = TokenBucketLimiter(1, 1, lambda: self.now)

    def dispatcher(self, headers=None):
        handler = object.__new__(Handler)
        handler.path = "/limited"
        handler.command = "POST"
        handler.headers = Message()
        handler.headers["Content-Length"] = "2"
        handler.headers["Content-Type"] = "application/json"
        for name, value in (headers or {}).items():
            handler.headers[name] = value
        handler.rfile = BytesIO(b"{}")
        handler.client_address = ("127.0.0.1", 1234)
        handler.request_id = "test"
        handler.responses = []
        handler._json = lambda *args: handler.responses.append(args)
        return handler

    @staticmethod
    def route(called):
        return {
            "method": "POST",
            "path": "/limited",
            "handler": lambda query, params, payload: (
                called.append(True) or 200,
                {"ok": True},
                {},
            ),
            "body": True,
            "idempotent": True,
            "rate_limited": True,
        }

    def test_limit_headers_denial_and_429_does_not_claim_idempotency(self):
        called = []
        handler = self.dispatcher({"Idempotency-Key": "retry-me"})
        store = IdempotencyStore()
        begin = Mock(wraps=store.begin)
        store.begin = begin
        with (
            patch("agent_qa.server.ROUTES", (self.route(called),)),
            patch("agent_qa.server.LIMITER", self.limiter),
            patch("agent_qa.server.IDEMPOTENCY_STORE", store),
            patch("agent_qa.server.authenticate_api_key", return_value=None),
        ):
            Handler._dispatch(handler)
            self.assertEqual(handler.responses[0][0], 200)
            self.assertEqual(handler._rate_limit_headers["RateLimit-Limit"], "1")

            second = self.dispatcher({"Idempotency-Key": "retry-after"})
            Handler._dispatch(second)

            self.now += 1
            third = self.dispatcher({"Idempotency-Key": "retry-after"})
            Handler._dispatch(third)

        self.assertEqual(second.responses[0][0], 429)
        self.assertEqual(second.responses[0][1]["error"]["code"], "rate_limited")
        self.assertEqual(
            second.responses[0][1]["error"]["message"], "Rate limit exceeded"
        )
        self.assertEqual(second.responses[0][2]["RateLimit-Remaining"], "0")
        self.assertGreaterEqual(int(second.responses[0][2]["Retry-After"]), 1)
        self.assertEqual(third.responses[0][0], 200)
        self.assertEqual(len(called), 2)
        self.assertEqual(begin.call_count, 2)

    def test_identity_priority_and_malformed_client_id_fallback(self):
        called = []
        route = self.route(called)
        for headers, auth_identity, expected in (
            ({"X-Client-Id": "customer-1"}, {"key_id": "key_2"}, "key:key_2"),
            ({"X-Client-Id": "customer-1"}, None, "client:customer-1"),
            ({"X-Client-Id": "bad id"}, None, "ip:127.0.0.1"),
        ):
            handler = self.dispatcher(headers)
            limiter = TokenBucketLimiter(2, 1, lambda: 1.0)
            with (
                patch("agent_qa.server.ROUTES", (route,)),
                patch("agent_qa.server.LIMITER", limiter),
                patch(
                    "agent_qa.server.authenticate_api_key",
                    return_value=auth_identity,
                ),
            ):
                Handler._dispatch(handler)
            self.assertIn(expected, limiter._buckets)

    def test_unauthorized_request_consumes_fallback_bucket(self):
        route = {
            "method": "GET",
            "path": "/limited",
            "handler": lambda *_args: (200, {}, {}),
            "role": "admin",
            "rate_limited": True,
        }
        first = self.dispatcher({"X-Client-Id": "bad-login"})
        first.command = "GET"
        first.path = "/limited"
        second = self.dispatcher({"X-Client-Id": "bad-login"})
        second.command = "GET"
        second.path = "/limited"
        with (
            patch("agent_qa.server.ROUTES", (route,)),
            patch("agent_qa.server.LIMITER", self.limiter),
            patch("agent_qa.server.authenticate_api_key", return_value=None),
        ):
            Handler._dispatch(first)
            Handler._dispatch(second)
        self.assertEqual(first.responses[0][0], 401)
        self.assertEqual(second.responses[0][0], 429)

    def test_rate_limit_denial_audit_retains_authenticated_actor(self):
        audit = AuditLog(10)
        identity = {"key_id": "key-42", "role": "admin"}
        called = []

        def auditing_dispatcher():
            handler = self.dispatcher()
            handler._request_started = 0.0
            handler._response_recorded = False

            def record_json(status, body, headers=None):
                handler.responses.append((status, body, headers or {}))
                handler._audit_response_body = body
                Handler._record_response(handler, status)

            handler._json = record_json
            return handler

        with (
            patch("agent_qa.server.ROUTES", (self.route(called),)),
            patch("agent_qa.server.LIMITER", self.limiter),
            patch("agent_qa.server.AUDIT_LOG", audit),
            patch("agent_qa.server.REGISTRY.record"),
            patch("agent_qa.server.REGISTRY.record_rate_limited"),
            patch("agent_qa.server.write_access_log"),
            patch("agent_qa.server.perf_counter", return_value=1.0),
            patch("agent_qa.server.authenticate_api_key", return_value=identity),
        ):
            first = auditing_dispatcher()
            self.addCleanup(clear_context)
            set_context(RequestContext("first-request"))
            Handler._dispatch(first)

            denied = auditing_dispatcher()
            set_context(RequestContext("denied-request"))
            Handler._dispatch(denied)

        self.assertEqual(first.responses[0][0], 200)
        self.assertEqual(denied.responses[0][0], 429)
        entry = audit.get(audit.last_seq)
        self.assertEqual(entry["status"], 429)
        self.assertEqual(entry["actor"], "key-42")

    def test_exempt_unmatched_and_method_mismatch_skip_buckets(self):
        called = []
        route = self.route(called)
        route["rate_limited"] = False
        with (
            patch("agent_qa.server.ROUTES", (route,)),
            patch("agent_qa.server.LIMITER", self.limiter),
            patch("agent_qa.server.authenticate_api_key", return_value=None),
        ):
            for _ in range(2):
                exempt = self.dispatcher({"X-Client-Id": "probe"})
                Handler._dispatch(exempt)
                self.assertEqual(exempt.responses[0][0], 200)

            missing = self.dispatcher()
            missing.path = "/missing"
            missing._rate_limit_headers = {"RateLimit-Limit": "stale"}
            Handler._dispatch(missing)
            wrong_method = self.dispatcher()
            wrong_method.command = "GET"
            wrong_method._rate_limit_headers = {"RateLimit-Limit": "stale"}
            Handler._dispatch(wrong_method)

        self.assertEqual(missing.responses[0][0], 404)
        self.assertEqual(wrong_method.responses[0][0], 405)
        self.assertEqual(missing._rate_limit_headers, {})
        self.assertEqual(wrong_method._rate_limit_headers, {})
        self.assertEqual(self.limiter.snapshot()["buckets"], 0)

    def test_json_writer_merges_rate_headers_onto_limited_responses(self):
        handler = object.__new__(Handler)
        handler.command = "GET"
        handler.request_id = "writer-test"
        handler.wfile = BytesIO()
        handler._rate_limit_headers = {
            "RateLimit-Limit": "2",
            "RateLimit-Remaining": "0",
            "RateLimit-Reset": "1",
            "Retry-After": "1",
        }
        sent_headers = []
        handler._record_response = lambda status: None
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: sent_headers.append((name, value))
        handler.end_headers = lambda: None

        Handler._json(
            handler,
            429,
            envelope("rate_limited", "Rate limit exceeded"),
        )

        self.assertIn(("RateLimit-Limit", "2"), sent_headers)
        self.assertIn(("RateLimit-Remaining", "0"), sent_headers)
        self.assertIn(("Retry-After", "1"), sent_headers)

    def test_env_policy_falls_back_without_import_side_effects(self):
        env = os.environ.copy()
        env["AGENT_QA_RATE_BURST"] = "100001"
        env["AGENT_QA_RATE_REFILL_PER_SECOND"] = "10001"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json; from agent_qa.ratelimit import LIMITER; "
                "print(json.dumps([LIMITER.burst, LIMITER.refill_per_second]))",
            ],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        self.assertEqual(json.loads(result.stdout), [120, 60.0])

    def test_env_policy_accepts_valid_bounded_values(self):
        env = os.environ.copy()
        env["AGENT_QA_RATE_BURST"] = "17"
        env["AGENT_QA_RATE_REFILL_PER_SECOND"] = "2.5"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json; from agent_qa.ratelimit import LIMITER; "
                "print(json.dumps([LIMITER.burst, LIMITER.refill_per_second]))",
            ],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        self.assertEqual(json.loads(result.stdout), [17, 2.5])


class RateLimitHttpApiTests(unittest.TestCase):
    """Exercise the HTTP API in an isolated local service process."""

    def setUp(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = os.environ.copy()
        env.update(
            {
                "APP_PORT": str(port),
                "AGENT_QA_API_KEY": "rate-limit-integration-admin",
                "AGENT_QA_RATE_BURST": "2",
                "AGENT_QA_RATE_REFILL_PER_SECOND": "0.001",
            }
        )
        self.process = subprocess.Popen(
            [sys.executable, "app.py"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                urlopen(self.base + "/ready", timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        self.process.terminate()
        self.process.wait(timeout=3)
        raise RuntimeError("rate-limit API service did not start")

    def tearDown(self):
        self.process.terminate()
        self.process.wait(timeout=3)

    def request(self, method, path, headers=None, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request_headers = dict(headers or {})
        if data is not None:
            request_headers.setdefault("Content-Type", "application/json")
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
            status = response.code
            response_headers = response.headers
            raw_body = response.read()
        try:
            body = json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = raw_body.decode("utf-8", errors="replace")
        return status, body, response_headers

    def admin(self, method, path, payload=None, extra_headers=None):
        headers = {"X-API-Key": "rate-limit-integration-admin"}
        headers.update(extra_headers or {})
        return self.request(method, path, headers, payload)

    def test_limited_headers_429_probes_and_unmatched_paths(self):
        headers = {"X-Client-Id": "http-burst"}
        first = self.request("GET", "/products", headers)
        second = self.request("GET", "/products", headers)
        denied = self.request("GET", "/products", headers)
        self.assertEqual(first[0], 200)
        self.assertEqual(first[2]["RateLimit-Limit"], "2")
        self.assertEqual(second[2]["RateLimit-Remaining"], "0")
        self.assertEqual(denied[0], 429)
        self.assertEqual(denied[1]["error"]["code"], "rate_limited")
        self.assertEqual(denied[1]["error"]["message"], "Rate limit exceeded")
        self.assertGreaterEqual(int(denied[2]["Retry-After"]), 1)

        for path in ("/ready", "/health", "/ping", "/metrics", "/status"):
            self.assertNotEqual(self.request("GET", path, headers)[0], 429)

        other = {"X-Client-Id": "no-missing-charge"}
        self.assertEqual(self.request("GET", "/missing-route", other)[0], 404)
        self.assertEqual(self.request("POST", "/ready", other)[0], 405)
        self.assertEqual(self.request("GET", "/products", other)[0], 200)
        self.assertEqual(self.request("GET", "/products", other)[0], 200)
        self.assertEqual(self.request("GET", "/products", other)[0], 429)

        metric = self.request("GET", "/metrics")[1]
        self.assertIn('agent_qa_rate_limited_total{kind="client"}', metric)
        self.assertNotIn("http-burst", metric)

    def test_invalid_key_consumes_client_fallback_before_401(self):
        self.admin(
            "PUT",
            "/admin/rate-limits/client:bad-key",
            {"burst": 1, "refill_per_second": 0.001},
        )
        headers = {"X-API-Key": "invalid-key", "X-Client-Id": "bad-key"}
        unauthorized = self.request("GET", "/admin/keys", headers)
        self.assertEqual(unauthorized[0], 401)
        self.assertEqual(unauthorized[2]["RateLimit-Limit"], "1")
        self.assertEqual(
            self.request("GET", "/products", {"X-Client-Id": "bad-key"})[0],
            429,
        )

    def test_identity_precedence_and_malformed_client_id_uses_ip(self):
        self.admin(
            "PUT",
            "/admin/rate-limits/key:bootstrap",
            {"burst": 1, "refill_per_second": 0.001},
        )
        self.admin(
            "PUT",
            "/admin/rate-limits/client:priority",
            {"burst": 2, "refill_per_second": 0.001},
        )
        both = {
            "X-API-Key": "rate-limit-integration-admin",
            "X-Client-Id": "priority",
        }
        self.assertEqual(self.request("GET", "/products", both)[0], 200)
        self.assertEqual(self.request("GET", "/products", both)[0], 429)
        self.assertEqual(
            self.request("GET", "/products", {"X-Client-Id": "priority"})[0],
            200,
        )

        self.admin(
            "PUT",
            "/admin/rate-limits/ip:127.0.0.1",
            {"burst": 1, "refill_per_second": 0.001},
        )
        malformed = {"X-Client-Id": "invalid id"}
        self.assertEqual(self.request("GET", "/products", malformed)[0], 200)
        self.assertEqual(self.request("GET", "/products", malformed)[0], 429)

    def test_admin_override_validation_role_limit_and_delete_flow(self):
        listed = self.admin("GET", "/admin/rate-limits")
        self.assertEqual(listed[0], 200)
        self.assertEqual(listed[1]["default"]["burst"], 2)
        self.assertEqual(
            self.admin(
                "PUT",
                "/admin/rate-limits/invalid",
                {"burst": 1, "refill_per_second": 1},
            )[0],
            400,
        )
        self.assertEqual(
            self.admin(
                "PUT",
                "/admin/rate-limits/client:bad-number",
                {"burst": True, "refill_per_second": 1},
            )[0],
            400,
        )

        put = self.admin(
            "PUT",
            "/admin/rate-limits/client:admin-flow",
            {"burst": 1, "refill_per_second": 0.001},
        )
        self.assertEqual(put[0], 200)
        self.assertEqual(put[1]["identity"], "client:admin-flow")
        self.assertEqual(
            self.request("GET", "/products", {"X-Client-Id": "admin-flow"})[0],
            200,
        )
        self.assertEqual(
            self.request("GET", "/products", {"X-Client-Id": "admin-flow"})[0],
            429,
        )
        overrides = self.admin("GET", "/admin/rate-limits")[1]["overrides"]
        self.assertEqual(overrides["client:admin-flow"]["burst"], 1)

        created = self.admin(
            "POST",
            "/admin/keys",
            {"role": "read", "label": "rate-reader"},
        )
        self.assertEqual(created[0], 201)
        self.assertEqual(
            self.request(
                "GET",
                "/admin/rate-limits",
                {"X-API-Key": created[1]["key"]},
            )[0],
            403,
        )

        for index in range(99):
            status, _, _ = self.admin(
                "PUT",
                f"/admin/rate-limits/client:cap-{index}",
                {"burst": 1, "refill_per_second": 1},
            )
            self.assertEqual(status, 200)
        self.assertEqual(
            self.admin(
                "PUT",
                "/admin/rate-limits/client:cap-overflow",
                {"burst": 1, "refill_per_second": 1},
            )[0],
            409,
        )
        self.assertEqual(
            self.admin("DELETE", "/admin/rate-limits/client:admin-flow")[0],
            204,
        )
        self.assertEqual(
            self.admin("DELETE", "/admin/rate-limits/client:admin-flow")[0],
            404,
        )
        self.assertEqual(
            self.request("GET", "/products", {"X-Client-Id": "admin-flow"})[0],
            200,
        )

    def test_429_does_not_claim_idempotency_key(self):
        headers = {"X-API-Key": "rate-limit-integration-admin"}
        payload = {
            "sku": "RL-TEST-001",
            "name": "Rate Limit Test",
            "category": "test",
            "price_cents": 100,
        }
        self.assertEqual(self.request("GET", "/products", headers)[0], 200)
        self.assertEqual(self.request("GET", "/products", headers)[0], 200)
        unused_key_headers = headers | {"Idempotency-Key": "ratelimit-product-new"}
        denied = self.request("POST", "/products", unused_key_headers, payload)
        self.assertEqual(denied[0], 429)
        reset = self.admin(
            "PUT",
            "/admin/rate-limits/key:bootstrap",
            {"burst": 2, "refill_per_second": 0.001},
        )
        self.assertEqual(reset[0], 200)
        created = self.request("POST", "/products", unused_key_headers, payload)
        self.assertEqual(created[0], 201)
        self.assertNotIn("Idempotent-Replay", created[2])
        replay = self.request("POST", "/products", unused_key_headers, payload)
        self.assertEqual(replay[0], 201)
        self.assertEqual(replay[2]["Idempotent-Replay"], "true")


if __name__ == "__main__":
    unittest.main()
