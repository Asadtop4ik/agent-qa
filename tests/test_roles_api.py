"""In-process coverage for route role checks and key management endpoints."""

import io
import json
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from time import perf_counter
from unittest.mock import patch

from agent_qa.errors import ApiError, envelope
from agent_qa.keys import KeyStore
from agent_qa.routes import ROUTES
from agent_qa.server import Handler


class Headers(dict):
    def get_all(self, name, default=None):
        value = self.get(name)
        return [value] if value is not None else (default or [])


class Harness:
    def __init__(self, method, path, body=b"not-json", key=None):
        self.command = method
        self.path = path
        self.request_id = "roles-test"
        self._request_started = perf_counter()
        self._response_recorded = False
        self.wfile = io.BytesIO()
        self.sent_headers = []
        self.headers = Headers()
        if key is not None:
            self.headers["X-API-Key"] = key
        if body is not None:
            self.headers["Content-Length"] = str(len(body))
            self.headers["Content-Type"] = "application/json"
        self.rfile = io.BytesIO(body or b"")
        self.responses = []

    def _json(self, status, body, headers=None):
        self.responses.append((status, body, headers or {}))
        Handler._json(self, status, body, headers)

    def send_response(self, status):
        self.sent_status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def _read_body(
        self,
        consumes=("application/json",),
        require_object=True,
        max_body_bytes=4096,
    ):
        return Handler._read_body(self, consumes, require_object, max_body_bytes)

    def _accepts(self, media_type):
        return Handler._accepts(self, media_type)

    def _record_response(self, status):
        Handler._record_response(self, status)

    def _not_found(self):
        self._json(404, {"error": {"code": "not_found"}})

    def _method_not_allowed(self, routes):
        self._json(405, {"error": {"code": "method_not_allowed"}})


class RoleApiTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.store = KeyStore("role-test-bootstrap", lambda: self.now)
        access_log_patch = patch("agent_qa.server.write_access_log")
        self.access_log = access_log_patch.start()
        self.addCleanup(access_log_patch.stop)
        self.read_key = self.store.create("read", "reader")["key"]
        self.write_key = self.store.create("write", "writer")["key"]
        self.admin_key = self.store.create("admin", "admin")["key"]
        self.credentials = (
            None,
            "invalid-secret",
            self.read_key,
            self.write_key,
            self.admin_key,
        )

    def dispatch(self, harness):
        try:
            Handler._dispatch(harness)
        except ApiError as error:
            harness._json(
                error.status,
                envelope(
                    error.code,
                    error.message,
                    error.details,
                    request_id=harness.request_id,
                ),
            )
        self.assertEqual(len(harness.responses), 1)
        return harness.responses[0]

    def test_every_route_enforces_its_declared_role_before_body_read(self):
        with patch("agent_qa.auth.KEY_STORE", self.store):
            for original in ROUTES:
                route = deepcopy(original)
                route["body"] = True
                route.pop("idempotent", None)
                route.pop("conditional_headers", None)
                route.pop("request_schema", None)
                route["handler"] = lambda *_args, **_kwargs: (204, None, {})
                path = route["path"]
                for part in ("{id}", "{name}", "{key_id}", "{seq}"):
                    path = path.replace(part, "sample")
                if route["path"].startswith(("/admin/keys", "/audit")):
                    required_role = "admin"
                elif route["path"] == "/whoami":
                    required_role = "read"
                elif route["path"] in {
                    "/exports/orders.csv",
                    "/exports/products.csv",
                }:
                    required_role = "read"
                elif route["method"] in {"POST", "PATCH", "DELETE"} and route[
                    "path"
                ].startswith(("/orders", "/products", "/imports")):
                    required_role = "write"
                else:
                    required_role = None
                self.assertEqual(route["role"], required_role)
                for key in self.credentials:
                    with self.subTest(path=route["path"], key=key is not None):
                        harness = Harness(route["method"], path, key=key)
                        with patch("agent_qa.server.ROUTES", (route,)):
                            status, body, headers = self.dispatch(harness)
                        if required_role is None:
                            expected = 400
                        elif key is None or key == "invalid-secret":
                            expected = 401
                        else:
                            identity = self.store.authenticate(key)
                            rank = {"read": 1, "write": 2, "admin": 3}
                            expected = (
                                403
                                if rank[identity["role"]] < rank[required_role]
                                else 415
                                if route["path"].startswith("/imports/")
                                else 400
                            )
                        self.assertEqual(status, expected)
                        if status in {401, 403}:
                            self.assertEqual(harness.rfile.tell(), 0)
                        if status == 401:
                            self.assertEqual(
                                headers.get("WWW-Authenticate"), "X-API-Key"
                            )
                        if status == 403:
                            self.assertEqual(body["error"]["code"], "forbidden")
                            self.assertEqual(
                                body["error"]["details"],
                                [
                                    {
                                        "field": "role",
                                        "message": f"Requires role {required_role}",
                                    }
                                ],
                            )

    def test_key_creation_rotation_grace_revoke_and_secret_redaction(self):
        with (
            patch("agent_qa.auth.KEY_STORE", self.store),
            patch("agent_qa.routes.auth.KEY_STORE", self.store),
        ):
            create_body = json.dumps({"role": "write", "label": "worker"}).encode()
            created = self.dispatch(
                Harness("POST", "/admin/keys", create_body, self.admin_key)
            )
            self.assertEqual(created[0], 201)
            original_secret = created[1]["key"]
            key_id = created[1]["key_id"]

            listing = self.dispatch(Harness("GET", "/admin/keys", None, self.admin_key))
            self.assertEqual(listing[0], 200)
            self.assertNotIn(original_secret, json.dumps(listing[1]))

            rotated = self.dispatch(
                Harness(
                    "POST",
                    f"/admin/keys/{key_id}/rotate",
                    b'{"grace_seconds":30}',
                    self.admin_key,
                )
            )
            self.assertEqual(rotated[0], 200)
            rotated_secret = rotated[1]["key"]
            for secret in (original_secret, rotated_secret):
                whoami = self.dispatch(Harness("GET", "/whoami", None, secret))
                self.assertEqual(whoami[0], 200)

            deleted = self.dispatch(
                Harness("DELETE", f"/admin/keys/{key_id}", None, self.admin_key)
            )
            self.assertEqual(deleted[0], 204)
            self.assertIsNone(self.store.authenticate(original_secret))
            self.assertIsNone(self.store.authenticate(rotated_secret))
            self.assertNotIn(original_secret, repr(listing[1]))
            self.assertNotIn(rotated_secret, repr(listing[1]))

    def test_invalid_create_inputs_and_bootstrap_routes_return_expected_errors(self):
        with (
            patch("agent_qa.auth.KEY_STORE", self.store),
            patch("agent_qa.routes.auth.KEY_STORE", self.store),
        ):
            invalid = self.dispatch(
                Harness(
                    "POST",
                    "/admin/keys",
                    b'{"role":[],"label":"x"}',
                    self.admin_key,
                )
            )
            self.assertEqual(invalid[0], 400)
            bootstrap = self.dispatch(
                Harness("POST", "/admin/keys/bootstrap/rotate", b"{}", self.admin_key)
            )
            self.assertEqual(bootstrap[0], 409)
            self.assertEqual(bootstrap[1]["error"]["code"], "bootstrap_key_immutable")

            delete_bootstrap = self.dispatch(
                Harness("DELETE", "/admin/keys/bootstrap", None, self.admin_key)
            )
            self.assertEqual(delete_bootstrap[0], 409)
            unknown = self.dispatch(
                Harness("DELETE", "/admin/keys/missing", None, self.admin_key)
            )
            self.assertEqual(unknown[0], 404)
            self.assertEqual(unknown[1]["error"]["code"], "key_not_found")

    def test_secret_never_enters_validation_errors_logs_or_metrics(self):
        with (
            patch("agent_qa.auth.KEY_STORE", self.store),
            patch("agent_qa.routes.auth.KEY_STORE", self.store),
        ):
            created = self.dispatch(
                Harness(
                    "POST",
                    "/admin/keys",
                    b'{"role":"write","label":"secret-check"}',
                    self.admin_key,
                )
            )
            secret = created[1]["key"]
            body = json.dumps({secret: "unknown-property"}).encode()
            error = self.dispatch(Harness("POST", "/admin/keys", body, self.admin_key))
            self.assertEqual(error[0], 400)
            self.assertNotIn(secret, repr(error[1]))
            self.assertNotIn(secret, repr(self.access_log.call_args_list))

            from agent_qa.metrics import REGISTRY

            self.assertNotIn(secret, REGISTRY.render(0, "roles-test"))

    def test_grace_expiry_and_malformed_grace_inputs_return_four_xx(self):
        with (
            patch("agent_qa.auth.KEY_STORE", self.store),
            patch("agent_qa.routes.auth.KEY_STORE", self.store),
        ):
            for invalid in (True, -1, 301, 10**1000):
                payload = json.dumps({"grace_seconds": invalid}).encode()
                result = self.dispatch(
                    Harness(
                        "POST",
                        "/admin/keys/key_1/rotate",
                        payload,
                        self.admin_key,
                    )
                )
                self.assertEqual(result[0], 400)

            created = self.dispatch(
                Harness(
                    "POST",
                    "/admin/keys",
                    b'{"role":"read","label":"grace"}',
                    self.admin_key,
                )
            )
            old_secret = created[1]["key"]
            key_id = created[1]["key_id"]
            rotated = self.dispatch(
                Harness(
                    "POST",
                    f"/admin/keys/{key_id}/rotate",
                    b'{"grace_seconds":1}',
                    self.admin_key,
                )
            )
            self.assertEqual(rotated[0], 200)
            self.now += timedelta(seconds=1)
            self.assertIsNone(self.store.authenticate(old_secret))

    def test_malformed_json_and_key_limit_fail_with_four_xx(self):
        with (
            patch("agent_qa.auth.KEY_STORE", self.store),
            patch("agent_qa.routes.auth.KEY_STORE", self.store),
        ):
            for body in (
                b'{"role":"read","label":"\xed\xa0\x80"}',
                b'{"role":"read","label":"\\ud800"}',
                b'{"x":' + b"[" * 34 + b"0" + b"]" * 34 + b"}",
                b'{"role":"read","label":"' + b" " * 41 + b'"}',
            ):
                result = self.dispatch(
                    Harness("POST", "/admin/keys", body, self.admin_key)
                )
                self.assertGreaterEqual(result[0], 400)
                self.assertLess(result[0], 500)

            for number in range(17):
                self.store.create("read", f"limit-{number}")
            limited = self.dispatch(
                Harness(
                    "POST",
                    "/admin/keys",
                    b'{"role":"read","label":"overflow"}',
                    self.admin_key,
                )
            )
            self.assertEqual(limited[0], 409)
            self.assertEqual(limited[1]["error"]["code"], "key_limit")


if __name__ == "__main__":
    unittest.main()
