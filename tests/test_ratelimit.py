"""Unit tests for the bounded token-bucket limiter."""

import json
import threading
import unittest
from contextlib import nullcontext
from email.message import Message
from io import BytesIO
from time import perf_counter
from unittest.mock import patch

from agent_qa.audit import AuditLog
from agent_qa.context import RequestContext, clear_context, set_context
from agent_qa.errors import ApiError
from agent_qa.metrics import MetricsRegistry
from agent_qa.ratelimit import RateLimitDecision, TokenBucketLimiter


class TokenBucketLimiterTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.limiter = TokenBucketLimiter(10, 2.0, clock=self.clock, capacity=4)

    def clock(self):
        return self.now

    def test_burst_refill_and_retry_after_round_up(self):
        first = self.limiter.consume("key:first", "key")
        self.assertTrue(first.allowed)
        self.assertEqual(first.limit, 10)
        self.assertEqual(first.remaining, 9)
        self.assertEqual(first.reset_after, 1)

        for _ in range(10):
            result = self.limiter.consume("key:first", "key")
        self.assertFalse(result.allowed)
        self.assertEqual(result.remaining, 0)
        self.assertEqual(result.retry_after, 1)

        self.now += 0.25
        result = self.limiter.consume("key:first", "key")
        self.assertFalse(result.allowed)
        self.assertEqual(result.retry_after, 1)
        self.now += 0.25
        self.assertTrue(self.limiter.consume("key:first", "key").allowed)

    def test_fractional_refill_and_full_reset(self):
        limiter = TokenBucketLimiter(2, 0.5, clock=self.clock)
        self.assertTrue(limiter.consume("client:x", "client").allowed)
        self.assertTrue(limiter.consume("client:x", "client").allowed)
        self.now += 1.0
        decision = limiter.consume("client:x", "client")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.retry_after, 1)
        self.now += 1.0
        self.assertTrue(limiter.consume("client:x", "client").allowed)

    def test_least_recently_used_full_bucket_is_evicted(self):
        limiter = TokenBucketLimiter(2, 1, clock=self.clock, capacity=2)
        limiter.consume("ip:a", "ip")
        limiter.consume("ip:b", "ip")
        self.now += 1
        limiter.consume("ip:a", "ip")  # refresh a's LRU position
        self.now += 1
        limiter.consume("ip:c", "ip")
        snapshot = limiter.snapshot()
        self.assertEqual(snapshot["buckets"], 2)
        self.assertIn("ip:a", limiter._buckets)
        self.assertIn("ip:c", limiter._buckets)
        self.assertNotIn("ip:b", limiter._buckets)

    def test_full_table_without_evictable_bucket_denies_unknown_identity(self):
        limiter = TokenBucketLimiter(2, 1, clock=self.clock, capacity=1)
        limiter.consume("key:a", "key")
        result = limiter.consume("key:b", "key")
        self.assertFalse(result.allowed)
        self.assertEqual(limiter.snapshot()["buckets"], 1)

    def test_overrides_reset_bucket_and_are_bounded(self):
        limiter = TokenBucketLimiter(2, 1, clock=self.clock, max_overrides=2)
        limiter.consume("key:a", "key")
        limiter.set_override("key:a", 3, 0.5)
        self.assertEqual(limiter.consume("key:a", "key").remaining, 2)
        limiter.set_override("client:b", 4, 1)
        with self.assertRaises(ApiError) as raised:
            limiter.set_override("ip:c", 5, 1)
        self.assertEqual(raised.exception.code, "override_limit")
        self.assertTrue(limiter.delete_override("key:a"))
        self.assertFalse(limiter.delete_override("key:a"))
        with self.assertRaises(ValueError):
            limiter.set_override("ip:c", 1, 10**1000)

    def test_full_table_admits_new_identity_after_existing_bucket_refills(self):
        limiter = TokenBucketLimiter(1, 1, clock=self.clock, capacity=1)
        limiter.consume("key:a", "key")
        self.assertFalse(limiter.consume("key:b", "key").allowed)
        self.now += 1
        self.assertTrue(limiter.consume("key:b", "key").allowed)

    def test_environment_defaults_reject_invalid_and_huge_values(self):
        from agent_qa.settings import load

        loaded = load(
            {
                "AGENT_QA_RATE_BURST": "9",
                "AGENT_QA_RATE_REFILL_PER_SECOND": "4.5",
            }
        )
        self.assertEqual(loaded.values["AGENT_QA_RATE_BURST"], 9)
        self.assertEqual(loaded.values["AGENT_QA_RATE_REFILL_PER_SECOND"], 4.5)
        for name, values in (
            ("AGENT_QA_RATE_BURST", ("0", "100001", "9" * 10000, "nope")),
            (
                "AGENT_QA_RATE_REFILL_PER_SECOND",
                ("nan", "inf", "0", "10001", "bad"),
            ),
        ):
            for value in values:
                with self.subTest(name=name, value=value[:12]):
                    result = load({name: value})
                    self.assertFalse(result.valid)

    def test_denials_are_audited_and_metrics_hide_identity(self):
        audit = AuditLog(2)
        entry = audit.append(
            RequestContext("request", actor="key_9", role="admin"),
            "POST",
            "/orders",
            "/orders",
            429,
        )
        self.assertEqual(entry["outcome"], "denied")

        metrics = MetricsRegistry()
        metrics.record_rate_limited("key")
        rendered = metrics.render(0, "test")
        self.assertIn('agent_qa_rate_limited_total{kind="key"} 1', rendered)
        self.assertNotIn("key_9", rendered)

    def test_server_denial_records_write_audit_and_bounded_metric(self):
        from agent_qa.server import Handler

        audit = AuditLog(4)
        responses = []
        handler = object.__new__(Handler)
        handler.path = "/limited-write"
        handler.command = "POST"
        handler.headers = Message()
        handler.headers["X-Client-Id"] = "audit-denial"
        handler.client_address = ("127.0.0.1", 12345)
        handler.request_id = "limited-request"
        handler._request_started = 1.0
        handler._response_recorded = False

        def capture(status, body, headers=None):
            handler._audit_response_body = body
            Handler._record_response(handler, status)
            responses.append((status, body, headers or {}))

        handler._json = capture
        route = {
            "method": "POST",
            "path": "/limited-write",
            "handler": lambda *_args: (201, {}, {}),
            "auth_required": True,
            "rate_limited": True,
        }
        set_context(RequestContext("limited-request"))
        try:
            with (
                patch("agent_qa.server.ROUTES", (route,)),
                patch("agent_qa.server.AUDIT_LOG", audit),
                patch("agent_qa.server.authenticate_api_key", return_value=None),
                patch(
                    "agent_qa.server.RATE_LIMITER.consume",
                    return_value=RateLimitDecision(False, 1, 0, 1, 1),
                ),
                patch("agent_qa.server.REGISTRY.record"),
                patch("agent_qa.server.REGISTRY.record_rate_limited") as metric,
                patch("agent_qa.server.write_access_log"),
            ):
                Handler._handle(handler)
        finally:
            clear_context()

        self.assertEqual(responses[0][0], 429)
        self.assertEqual(audit.get(1)["outcome"], "denied")
        metric.assert_called_once_with("client")

    def test_parallel_burst_has_exactly_ten_permits(self):
        limiter = TokenBucketLimiter(10, 1, clock=self.clock)
        barrier = threading.Barrier(50)
        results = []
        lock = threading.Lock()

        def consume():
            barrier.wait(timeout=2)
            result = limiter.consume("key:parallel", "key").allowed
            with lock:
                results.append(result)

        threads = [threading.Thread(target=consume) for _ in range(50)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sum(results), 10)


class RateLimitHandlerIntegrationTests(unittest.TestCase):
    """Exercise route dispatch and the shared limiter without opening sockets."""

    def setUp(self):
        from agent_qa.audit import AuditLog

        self.audit = AuditLog(10)

    def dispatch(
        self,
        method,
        path,
        *,
        headers=None,
        body=None,
        identity=None,
        routes=None,
    ):
        from agent_qa.server import Handler

        handler = object.__new__(Handler)
        handler.command = method
        handler.path = path
        handler.headers = Message()
        for name, value in (headers or {}).items():
            handler.headers[name] = value
        raw_body = b"" if body is None else json.dumps(body).encode("utf-8")
        if body is not None:
            handler.headers["Content-Length"] = str(len(raw_body))
            handler.headers["Content-Type"] = "application/json"
        handler.rfile = BytesIO(raw_body)
        handler.client_address = ("127.0.0.1", 12345)
        handler.request_id = "rate-limit-unit-request"
        handler._request_started = perf_counter()
        handler._response_recorded = False
        handler.wfile = BytesIO()
        result = {"headers": {}}
        handler.send_response = lambda status: result.update(status=status)
        handler.send_header = lambda name, value: result["headers"].update(
            {name: value}
        )
        handler.end_headers = lambda: None

        def write_json(status, response_body, response_headers=None):
            return Handler._json(handler, status, response_body, response_headers)

        handler._json = write_json
        set_context(RequestContext(handler.request_id))
        try:
            with (
                patch("agent_qa.server.AUDIT_LOG", self.audit),
                patch("agent_qa.server.REGISTRY.record"),
                patch("agent_qa.server.write_access_log"),
                patch("agent_qa.server.authenticate_api_key", return_value=identity),
                patch("agent_qa.server.ROUTES", routes)
                if routes is not None
                else nullcontext(),
            ):
                Handler._handle(handler)
        finally:
            clear_context()
        response = handler.wfile.getvalue()
        result["body"] = json.loads(response) if response else None
        return result

    def override(self, identity, burst, refill=0.001):
        from agent_qa.ratelimit import RATE_LIMITER

        RATE_LIMITER.set_override(identity, burst, refill)

    def remove_override(self, identity):
        from agent_qa.ratelimit import RATE_LIMITER

        RATE_LIMITER.delete_override(identity)

    def test_headers_auth_denial_exemptions_and_unmatched_dispatch(self):
        identity = "client:socketless-auth"
        self.override(identity, 2)
        try:
            first = self.dispatch(
                "GET",
                "/audit",
                headers={"X-Client-Id": "socketless-auth", "X-API-Key": "bad"},
            )
            self.assertEqual(first["status"], 401)
            self.assertEqual(first["headers"]["RateLimit-Limit"], "2")
            self.assertEqual(first["headers"]["RateLimit-Remaining"], "1")
            second = self.dispatch(
                "GET",
                "/audit",
                headers={"X-Client-Id": "socketless-auth", "X-API-Key": "bad"},
            )
            self.assertEqual(second["status"], 401)
            denied = self.dispatch(
                "GET",
                "/audit",
                headers={"X-Client-Id": "socketless-auth", "X-API-Key": "bad"},
            )
            self.assertEqual(denied["status"], 429)
            self.assertEqual(denied["headers"]["Retry-After"], "1000")
            self.assertEqual(denied["body"]["error"]["code"], "rate_limited")

            for _ in range(3):
                ready = self.dispatch("GET", "/ready")
                self.assertEqual(ready["status"], 200)
                self.assertNotIn("RateLimit-Limit", ready["headers"])
            missing = self.dispatch("GET", "/no-such-route")
            wrong_method = self.dispatch("PUT", "/ready")
            self.assertEqual(missing["status"], 404)
            self.assertEqual(wrong_method["status"], 405)
            self.assertNotIn("RateLimit-Limit", missing["headers"])
            self.assertNotIn("RateLimit-Limit", wrong_method["headers"])
        finally:
            self.remove_override(identity)

    def test_key_client_and_ip_identity_precedence(self):
        key_identity = "key:socket-key"
        client_identity = "client:socket-client"
        ip_identity = "ip:127.0.0.1"
        self.override(key_identity, 1)
        self.override(client_identity, 1)
        self.override(ip_identity, 1)
        try:
            valid_key = {"key_id": "socket-key", "role": "admin", "label": "test"}
            keyed = self.dispatch(
                "GET",
                "/orders",
                headers={"X-API-Key": "valid", "X-Client-Id": "one"},
                identity=valid_key,
            )
            self.assertEqual(keyed["status"], 200)
            key_denied = self.dispatch(
                "GET",
                "/orders",
                headers={"X-API-Key": "valid", "X-Client-Id": "two"},
                identity=valid_key,
            )
            self.assertEqual(key_denied["status"], 429)

            # An invalid client ID uses the IP bucket; a valid ID gets its own bucket.
            ip_request = self.dispatch(
                "GET", "/orders", headers={"X-Client-Id": "bad/id"}
            )
            self.assertEqual(ip_request["status"], 200)
            ip_denied = self.dispatch(
                "GET", "/orders", headers={"X-Client-Id": "bad/other"}
            )
            self.assertEqual(ip_denied["status"], 429)
            client_request = self.dispatch(
                "GET", "/orders", headers={"X-Client-Id": "socket-client"}
            )
            self.assertEqual(client_request["status"], 200)
            client_denied = self.dispatch(
                "GET", "/orders", headers={"X-Client-Id": "socket-client"}
            )
            self.assertEqual(client_denied["status"], 429)
        finally:
            for identity in (key_identity, client_identity, ip_identity):
                self.remove_override(identity)

    def test_429_does_not_reserve_idempotency_key_and_write_is_audited(self):
        identity = "client:socketless-idempotency"
        self.override(identity, 1)
        route = {
            "method": "POST",
            "path": "/unit-idempotent",
            "handler": lambda *_args: (201, {"created": True}, {}),
            "role": "write",
            "auth_required": True,
            "rate_limited": True,
            "idempotent": True,
        }
        headers = {
            "X-Client-Id": "socketless-idempotency",
            "X-API-Key": "valid-write-key",
            "Idempotency-Key": "rate-limit-before-idempotency",
        }
        identity_data = {
            "key_id": "socket-write",
            "role": "write",
            "label": "test",
        }
        try:
            unauthorized = self.dispatch(
                "POST", "/unit-idempotent", headers=headers, routes=(route,)
            )
            self.assertEqual(unauthorized["status"], 401)
            denied = self.dispatch(
                "POST", "/unit-idempotent", headers=headers, routes=(route,)
            )
            self.assertEqual(denied["status"], 429)
            self.assertEqual(self.audit.get(2)["outcome"], "denied")

            self.override(identity, 2)
            created = self.dispatch(
                "POST",
                "/unit-idempotent",
                headers=headers,
                identity=identity_data,
                routes=(route,),
            )
            self.assertEqual(created["status"], 201)
            self.assertNotIn("Idempotent-Replay", created["headers"])
        finally:
            self.remove_override(identity)


if __name__ == "__main__":
    unittest.main()
