"""Socket-free checks for request audit recording in the HTTP adapter."""

from email.message import Message
from io import BytesIO
import unittest
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from unittest.mock import patch

from agent_qa.audit import AuditLog
from agent_qa.context import (
    RequestContext,
    clear_context,
    get_context,
    set_context,
)
from agent_qa.server import Handler


class AuditRequestRecordingTests(unittest.TestCase):
    def setUp(self):
        self.audit = AuditLog(10)
        self.audit_patch = patch("agent_qa.server.AUDIT_LOG", self.audit)
        self.audit_patch.start()
        self.metrics_patch = patch("agent_qa.server.REGISTRY.record")
        self.metrics_patch.start()
        self.access_patch = patch("agent_qa.server.write_access_log")
        self.access_patch.start()

    def tearDown(self):
        clear_context()
        self.access_patch.stop()
        self.metrics_patch.stop()
        self.audit_patch.stop()

    def record(
        self, method, path, status, *, actor="key-1", role="admin", replay=False
    ):
        context = RequestContext(
            "request-1", actor=actor, role=role, resource="orders", resource_id=7
        )
        set_context(context)
        handler = object.__new__(Handler)
        handler.command = method
        handler.path = path
        handler.request_id = "request-1"
        handler._request_started = 1.0
        handler._response_recorded = False
        if replay:
            handler._audit_replay = True
        with patch("agent_qa.server.perf_counter", return_value=2.0):
            Handler._record_response(handler, status)

    def test_matched_write_errors_are_logged_with_outcome_and_safe_path(self):
        for status, outcome in (
            (201, "success"),
            (401, "denied"),
            (403, "denied"),
            (404, "rejected"),
            (409, "rejected"),
            (500, "error"),
        ):
            self.record("POST", "/orders?customer=private", status)
            entry = self.audit.get(self.audit.last_seq)
            self.assertEqual(entry["status"], status)
            self.assertEqual(entry["outcome"], outcome)
            self.assertEqual(entry["route"], "/orders")
            self.assertEqual(entry["path"], "/orders")
            self.assertEqual(entry["resource_id"], 7)

    def test_anonymous_denial_get_405_and_unmatched_are_handled_correctly(self):
        self.record("POST", "/orders", 401, actor="anonymous", role=None)
        self.assertEqual(self.audit.get(1)["actor"], "anonymous")
        self.assertIsNone(self.audit.get(1)["role"])
        self.record("GET", "/orders", 200)
        self.record("PUT", "/orders", 405)
        self.record("POST", "/no-such-route", 404)
        self.assertEqual(self.audit.last_seq, 1)

    def test_matched_handler_405_is_never_audited(self):
        handler = object.__new__(Handler)
        handler.command = "POST"
        handler.path = "/synthetic"
        handler.request_id = "request-405"
        handler._request_started = 1.0
        handler._response_recorded = False
        route = {"method": "POST", "path": "/synthetic"}
        with (
            patch("agent_qa.server.ROUTES", (route,)),
            patch("agent_qa.server.perf_counter", return_value=2.0),
        ):
            Handler._record_response(handler, 405)
        self.assertEqual(self.audit.last_seq, 0)

    def test_replay_marker_is_written_only_when_requested(self):
        self.record("POST", "/orders", 201, replay=True)
        self.record("POST", "/orders", 201)
        self.assertIs(self.audit.get(1)["replay"], True)
        self.assertNotIn("replay", self.audit.get(2))

    def test_context_is_thread_local_and_can_be_cleared(self):
        set_context(RequestContext("main"))
        seen = []
        worker = threading.Thread(target=lambda: seen.append(get_context()))
        worker.start()
        worker.join(timeout=3)
        self.assertEqual(seen, [None])
        clear_context()

    def test_context_is_cleared_when_base_request_handler_raises(self):
        handler = object.__new__(Handler)
        handler._tenant_scoped = True
        handler._tenant_value = "previous"

        def fail_request():
            self.assertIsNotNone(get_context())
            self.assertFalse(handler._tenant_scoped)
            self.assertIsNone(handler._tenant_value)
            raise RuntimeError("simulated request parser failure")

        with patch(
            "agent_qa.server.BaseHTTPRequestHandler.handle_one_request",
            side_effect=fail_request,
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                Handler.handle_one_request(handler)
        self.assertIsNone(get_context())

    def test_matching_write_auth_denial_is_appended_before_response(self):
        handler = object.__new__(Handler)
        handler.command = "POST"
        handler.path = "/orders"
        handler.request_id = "denied-request"
        handler.headers = Message()
        handler._request_started = 1.0
        handler._response_recorded = False
        response_observations = []

        def sent(status):
            response_observations.append((status, self.audit.last_seq))

        handler.send_response = sent
        handler.send_header = lambda *_args: None
        handler.end_headers = lambda: None
        handler.wfile = BytesIO()
        with patch("agent_qa.server.perf_counter", return_value=2.0):
            Handler._dispatch(handler)
        self.assertEqual(response_observations, [(401, 1)])
        entry = self.audit.get(1)
        self.assertEqual(entry["actor"], "anonymous")
        self.assertIsNone(entry["role"])

    def test_valid_identity_is_recorded_for_public_write_routes(self):
        handler = object.__new__(Handler)
        handler.command = "POST"
        handler.path = "/public-write"
        handler.request_id = "public-write"
        handler.headers = Message()
        handler._json = lambda *_args: None
        route = {
            "method": "POST",
            "path": "/public-write",
            "handler": lambda *_args: (201, {"ok": True}, {}),
            "auth_required": False,
            "rate_limited": False,
        }
        identity = {"key_id": "key-public", "role": "read", "label": "public"}
        set_context(RequestContext("public-write"))
        with (
            patch("agent_qa.server.ROUTES", (route,)),
            patch(
                "agent_qa.server.authenticate_api_key", return_value=identity
            ) as auth,
        ):
            Handler._dispatch(handler)
        auth.assert_called_once_with(handler.headers)
        context = get_context()
        self.assertEqual(context.actor, "key-public")
        self.assertEqual(context.role, "read")


class AuditApiSubprocessTests(unittest.TestCase):
    """End-to-end flows; intentionally excluded from sandbox execution."""

    @classmethod
    def setUpClass(cls):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        cls.admin_key = "audit-bootstrap-test-secret"
        env = {
            "APP_PORT": str(cls.port),
            "AGENT_QA_API_KEY": cls.admin_key,
            "AGENT_QA_AUDIT_CAPACITY": "50",
            "AGENT_QA_GIT_SHA": "audit-api-test",
        }
        cls.process = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=Path(__file__).resolve().parents[1],
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
            except (OSError, URLError):
                time.sleep(0.05)
        cls.stop_process()
        raise RuntimeError("audit API service did not start")

    @classmethod
    def tearDownClass(cls):
        cls.stop_process()

    @classmethod
    def stop_process(cls):
        if cls.process.poll() is None:
            cls.process.terminate()
        try:
            cls.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=3)

    def request(self, method, path, payload=None, *, key=None, headers=None):
        request_headers = dict(headers or {})
        if key is not None:
            request_headers["X-API-Key"] = key
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        if data is not None:
            request_headers["Content-Type"] = "application/json"
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
            raw = response.read()
            return response.status, json.loads(raw) if raw else None

    def test_write_outcomes_replay_and_unmatched_requests(self):
        baseline = self.request("GET", "/audit", key=self.admin_key)[1]["last_seq"]
        payload = {"customer_id": "audit-customer", "total_cents": 1200}
        headers = {"Idempotency-Key": "audit-retry"}
        self.assertEqual(
            self.request(
                "POST", "/orders", payload, key=self.admin_key, headers=headers
            )[0],
            201,
        )
        self.assertEqual(
            self.request(
                "POST", "/orders", payload, key=self.admin_key, headers=headers
            )[0],
            201,
        )
        self.assertEqual(
            self.request(
                "POST",
                "/orders",
                {"unknown": "private-body"},
                key=self.admin_key,
            )[0],
            400,
        )
        self.assertEqual(self.request("POST", "/orders", payload)[0], 401)
        self.assertEqual(self.request("PUT", "/ready", {}, key=self.admin_key)[0], 405)
        self.assertEqual(
            self.request("POST", "/missing", {}, key=self.admin_key)[0], 404
        )
        before = self.request(
            "GET", f"/audit?since_seq={baseline}", key=self.admin_key
        )[1]
        items = before["items"]
        self.assertEqual(before["last_seq"], baseline + 4)
        self.assertEqual(
            [entry["outcome"] for entry in items],
            ["denied", "rejected", "success", "success"],
        )
        self.assertEqual(items[0]["actor"], "anonymous")
        self.assertNotIn("replay", items[0])
        self.assertIs(items[2]["replay"], True)
        self.assertNotIn("replay", items[3])
        self.assertNotIn("private-body", json.dumps(before))
        self.assertEqual(
            self.request("GET", "/audit", key=self.admin_key)[1]["last_seq"],
            baseline + 4,
        )

    def test_audit_admin_query_validation_and_detail_lookup(self):
        baseline = self.request("GET", "/audit", key=self.admin_key)[1]["last_seq"]
        created = self.request(
            "POST",
            "/admin/keys",
            {"role": "read", "label": "audit-reader"},
            key=self.admin_key,
        )
        self.assertEqual(created[0], 201)
        key_body = created[1]
        audit_json = json.dumps(self.request("GET", "/audit", key=self.admin_key)[1])
        self.assertNotIn(key_body["key"], audit_json)
        self.assertEqual(self.request("GET", "/audit", key=key_body["key"])[0], 403)
        status, result = self.request(
            "GET",
            f"/audit?method=POST&resource=keys&since_seq={baseline}&limit=1&order=asc",
            key=self.admin_key,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["total_matching"], 1)
        self.assertEqual(result["items"][0]["resource_id"], key_body["key_id"])
        seq = result["items"][0]["seq"]
        self.assertEqual(
            self.request("GET", f"/audit/{seq}", key=self.admin_key)[0], 200
        )
        missing = self.request("GET", "/audit/999999", key=self.admin_key)[1]
        self.assertEqual(missing["error"]["code"], "audit_entry_not_found")
        self.assertEqual(
            self.request("GET", "/audit?limit=999", key=self.admin_key)[0], 400
        )
        self.assertEqual(
            self.request("GET", "/audit?limit=1&limit=2", key=self.admin_key)[0], 400
        )


if __name__ == "__main__":
    unittest.main()
