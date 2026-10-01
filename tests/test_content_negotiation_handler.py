"""Socket-free tests for HTTP response negotiation in the handler."""

import gzip
import io
import json
import unittest
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_qa.conditional import parse_etag_list, weak_match
from agent_qa.errors import ApiError, envelope
from agent_qa.schemas import V2_CREATE_ORDER_SCHEMA
from agent_qa.server import Handler


def make_handler(path="/fixture", method="GET", headers=None):
    handler = object.__new__(Handler)
    handler.path = path
    handler.command = method
    handler.headers = Message()
    for name, value in (headers or {}).items():
        handler.headers[name] = value
    handler.request_id = "request-123"
    handler._rate_headers = {}
    handler._response_recorded = False
    handler._request_started = 0
    handler.rfile = io.BytesIO(b"{}")
    handler.wfile = io.BytesIO()
    handler.sent_headers = []
    handler.status = None
    handler.send_response = lambda status: setattr(handler, "status", status)
    handler.send_header = lambda name, value: handler.sent_headers.append((name, value))
    handler.end_headers = lambda: None
    handler._record_response = lambda status: None
    return handler


def response_header(handler, name):
    values = [
        value for key, value in handler.sent_headers if key.lower() == name.lower()
    ]
    return values[-1] if values else None


class ContentNegotiationHandlerTests(unittest.TestCase):
    def test_version_headers_are_generated_and_successor_link_is_merged(self):
        route = {
            "path": "/fixture/{id}",
            "method": "GET",
            "api_version": "1",
            "deprecated": True,
            "successor": "/v2/fixture/{id}",
        }
        handler = make_handler("/fixture/17", headers={"Accept": "application/json"})
        with patch("agent_qa.server.ROUTES", (route,)):
            Handler._json(
                handler,
                200,
                {"ok": True},
                {"Link": '</next>; rel="next"'},
            )
        self.assertEqual(response_header(handler, "Deprecation"), "@1790812800")
        self.assertEqual(
            response_header(handler, "Sunset"), "Thu, 31 Dec 2026 23:59:59 GMT"
        )
        self.assertEqual(response_header(handler, "X-API-Version"), "1")
        self.assertEqual(
            response_header(handler, "Link"),
            '</next>; rel="next", </v2/fixture/17>; rel="successor-version"',
        )

        error_handler = make_handler(
            "/fixture/17", headers={"Accept": "application/json"}
        )
        with patch("agent_qa.server.ROUTES", (route,)):
            Handler._json(error_handler, 404, envelope("missing", "Missing"))
        self.assertEqual(response_header(error_handler, "Deprecation"), "@1790812800")
        self.assertEqual(response_header(error_handler, "X-API-Version"), "1")

    def test_v2_errors_always_use_problem_json(self):
        route = {"path": "/v2/fixture", "method": "GET", "api_version": "2"}
        handler = make_handler("/v2/fixture", headers={"Accept": "application/json"})
        with patch("agent_qa.server.ROUTES", (route,)):
            Handler._json(handler, 400, envelope("invalid", "Invalid"))
        self.assertEqual(
            response_header(handler, "Content-Type"),
            "application/problem+json; charset=utf-8",
        )
        self.assertEqual(response_header(handler, "X-API-Version"), "2")

    def test_v2_method_mismatch_still_enforces_json_acceptability(self):
        route = {
            "path": "/v2/orders",
            "method": "GET",
            "api_version": "2",
            "role": None,
            "auth_required": False,
        }
        for accept, expected_status in (
            ("text/html", 406),
            ("application/json", 405),
        ):
            with self.subTest(accept=accept):
                handler = make_handler(
                    "/v2/orders", method="POST", headers={"Accept": accept}
                )
                with patch("agent_qa.server.ROUTES", (route,)):
                    Handler._handle(handler)
                self.assertEqual(handler.status, expected_status)
                self.assertEqual(response_header(handler, "X-API-Version"), "2")
                self.assertEqual(
                    response_header(handler, "Content-Type"),
                    "application/problem+json; charset=utf-8",
                )

    def test_v2_dispatch_validation_reports_nested_v2_fields(self):
        route = {
            "path": "/v2/orders",
            "method": "POST",
            "api_version": "2",
            "body": True,
            "request_schema": V2_CREATE_ORDER_SCHEMA,
            "role": None,
            "auth_required": False,
            "rate_limited": False,
        }
        payload = b'{"customer":{},"amount":{"total_cents":true}}'
        handler = make_handler(
            "/v2/orders",
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        handler.rfile = io.BytesIO(payload)
        with patch("agent_qa.server.ROUTES", (route,)):
            with patch("agent_qa.server.authenticate_api_key", return_value=None):
                Handler._handle(handler)
        response = json.loads(handler.wfile.getvalue())
        self.assertEqual(handler.status, 400)
        self.assertEqual(
            {item["field"] for item in response["errors"]},
            {"customer.id", "amount.total_cents"},
        )
        self.assertEqual(
            response_header(handler, "Content-Type"),
            "application/problem+json; charset=utf-8",
        )

    def test_csv_body_reader_strips_bom_and_enforces_charset_and_utf8(self):
        handler = make_handler(headers={"Content-Type": "text/csv; charset=utf-8"})
        csv_body = b"\xef\xbb\xbfsku,name\r\na,Alpha\r\n"
        handler.rfile = io.BytesIO(csv_body)
        handler.headers["Content-Length"] = str(len(csv_body))
        self.assertEqual(
            Handler._read_request_body(
                handler,
                ("text/csv",),
                max_body_bytes=128,
            ),
            "sku,name\r\na,Alpha\r\n",
        )

        handler = make_handler(headers={"Content-Type": "text/csv; charset=latin-1"})
        handler.headers["Content-Length"] = "0"
        with self.assertRaises(ApiError) as caught:
            Handler._read_request_body(handler, ("text/csv",), max_body_bytes=128)
        self.assertEqual(caught.exception.status, 415)

        handler = make_handler(headers={"Content-Type": "text/csv"})
        handler.rfile = io.BytesIO(b"\xff")
        handler.headers["Content-Length"] = "1"
        with self.assertRaises(ApiError) as caught:
            Handler._read_request_body(handler, ("text/csv",), max_body_bytes=128)
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.code, "invalid_csv")
        self.assertEqual(caught.exception.details[0]["field"], "body")

    def test_problem_error_is_selected_and_has_accept_vary(self):
        handler = make_handler(
            "/missing?private=1",
            headers={"Accept": "application/problem+json, application/json"},
        )

        Handler._json(
            handler,
            404,
            {"error": {"code": "not_found", "message": "Route not found"}},
        )

        body = json.loads(handler.wfile.getvalue())
        self.assertEqual(handler.status, 404)
        self.assertEqual(
            response_header(handler, "Content-Type"),
            "application/problem+json; charset=utf-8",
        )
        self.assertEqual(body["instance"], "/missing")
        self.assertEqual(body["code"], "not_found")
        self.assertEqual(response_header(handler, "Vary"), "Accept, Accept-Encoding")

    def test_default_error_envelope_is_kept(self):
        handler = make_handler("/missing")
        Handler._json(
            handler,
            404,
            {"error": {"code": "not_found", "message": "Route not found"}},
        )
        self.assertEqual(
            json.loads(handler.wfile.getvalue()),
            {"error": {"code": "not_found", "message": "Route not found"}},
        )
        self.assertEqual(response_header(handler, "Vary"), "Accept, Accept-Encoding")

    def test_gzip_is_deterministic_has_length_vary_and_weak_etag(self):
        body = {"value": "x" * 400}
        handler = make_handler(
            "/fixture",
            headers={"Accept-Encoding": "gzip", "If-None-Match": '"canonical"'},
        )
        Handler._json(
            handler,
            200,
            body,
            {"ETag": '"canonical"', "Vary": "Origin"},
        )
        encoded = handler.wfile.getvalue()
        expected = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.assertEqual(gzip.decompress(encoded), expected)
        self.assertEqual(response_header(handler, "Content-Encoding"), "gzip")
        self.assertEqual(response_header(handler, "Content-Length"), str(len(encoded)))
        self.assertEqual(response_header(handler, "ETag"), 'W/"canonical"')
        self.assertEqual(response_header(handler, "Vary"), "Origin, Accept-Encoding")

        second = make_handler("/fixture", headers={"Accept-Encoding": "gzip"})
        Handler._json(second, 200, body)
        self.assertEqual(second.wfile.getvalue(), encoded)

    def test_small_and_ready_responses_are_not_compressed(self):
        for path, body in (
            ("/fixture", {"ok": True}),
            ("/ready", {"value": "x" * 400}),
        ):
            with self.subTest(path=path):
                handler = make_handler(path, headers={"Accept-Encoding": "gzip"})
                Handler._json(handler, 200, body)
                self.assertIsNone(response_header(handler, "Content-Encoding"))
                if path == "/ready":
                    self.assertEqual(
                        handler.wfile.getvalue(),
                        json.dumps(body, separators=(",", ":")).encode("utf-8"),
                    )

    def test_uncompressed_body_still_varies_on_accept_encoding(self):
        handler = make_handler("/fixture", headers={"Accept-Encoding": "identity"})
        Handler._json(handler, 200, {"ok": True})
        self.assertEqual(response_header(handler, "Vary"), "Accept-Encoding")

    def test_gzip_threshold_quality_wildcard_and_empty_responses(self):
        for size, should_compress in ((255, False), (256, True)):
            with self.subTest(size=size):
                handler = make_handler(
                    "/fixture",
                    headers={"Accept-Encoding": "gzip"},
                )
                Handler._json(
                    handler,
                    200,
                    "x" * size,
                    {"Content-Type": "text/plain"},
                )
                self.assertEqual(
                    response_header(handler, "Content-Encoding") == "gzip",
                    should_compress,
                )

        for value, should_compress in (
            ("gzip;q=0", False),
            ("*;q=1", True),
        ):
            with self.subTest(accept_encoding=value):
                handler = make_handler("/fixture", headers={"Accept-Encoding": value})
                Handler._json(handler, 200, "x" * 300, {"Content-Type": "text/plain"})
                self.assertEqual(
                    response_header(handler, "Content-Encoding") == "gzip",
                    should_compress,
                )

        for status in (204, 304):
            handler = make_handler("/fixture", headers={"Accept-Encoding": "gzip"})
            Handler._json(handler, status, None)
            self.assertIsNone(response_header(handler, "Content-Encoding"))
            self.assertEqual(handler.wfile.getvalue(), b"")

    def test_vary_merge_is_case_insensitive(self):
        handler = make_handler(
            "/missing?x=1",
            headers={"Accept": "application/problem+json"},
        )
        Handler._json(
            handler,
            400,
            {"error": {"code": "invalid", "message": "Invalid"}},
            {"vary": "origin, ACCEPT"},
        )
        self.assertEqual(
            response_header(handler, "Vary"), "origin, ACCEPT, Accept-Encoding"
        )

    def test_error_headers_and_details_survive_problem_formatting(self):
        cases = (
            (401, {"WWW-Authenticate": "X-API-Key"}),
            (405, {"Allow": "GET, HEAD"}),
            (429, {"Retry-After": "3"}),
        )
        for status, headers in cases:
            with self.subTest(status=status):
                handler = make_handler(
                    "/dispatch-test",
                    headers={"Accept": "application/problem+json"},
                )
                Handler._json(
                    handler,
                    status,
                    {
                        "error": {
                            "code": "test_error",
                            "message": "Request failed",
                            "details": [{"field": "role", "message": "Required"}],
                        }
                    },
                    headers,
                )
                body = json.loads(handler.wfile.getvalue())
                self.assertEqual(body["status"], status)
                self.assertEqual(body["errors"][0]["field"], "role")
                self.assertEqual(
                    response_header(handler, next(iter(headers))),
                    next(iter(headers.values())),
                )

    def test_auth_rate_limit_and_internal_errors_use_problem_format(self):
        route = {
            "path": "/dispatch-test",
            "method": "GET",
            "handler": Mock(return_value=(200, {"ok": True}, {})),
            "role": "admin",
            "auth_required": True,
            "rate_limited": False,
        }
        for identity, status in ((None, 401), ({"role": "read"}, 403)):
            handler = make_handler(
                "/dispatch-test", headers={"Accept": "application/problem+json"}
            )
            with (
                patch("agent_qa.server.ROUTES", (route,)),
                patch("agent_qa.server.authenticate_api_key", return_value=identity),
            ):
                Handler._handle(handler, lambda: Handler._dispatch(handler))
            self.assertEqual(handler.status, status)
            self.assertEqual(json.loads(handler.wfile.getvalue())["status"], status)

        limited_route = dict(route, role=None, auth_required=False, rate_limited=True)
        handler = make_handler(
            "/dispatch-test", headers={"Accept": "application/problem+json"}
        )
        decision = SimpleNamespace(
            allowed=False, limit=10, remaining=0, reset_after=1, retry_after=3
        )
        with (
            patch("agent_qa.server.ROUTES", (limited_route,)),
            patch("agent_qa.server.RATE_LIMITER.consume", return_value=decision),
        ):
            Handler._handle(handler, lambda: Handler._dispatch(handler))
        self.assertEqual(handler.status, 429)
        self.assertEqual(json.loads(handler.wfile.getvalue())["status"], 429)
        self.assertEqual(response_header(handler, "Retry-After"), "3")

        broken_route = dict(route, role=None, auth_required=False)
        broken_route["handler"] = Mock(side_effect=RuntimeError("unexpected"))
        handler = make_handler(
            "/dispatch-test", headers={"Accept": "application/problem+json"}
        )
        with (
            patch("agent_qa.server.ROUTES", (broken_route,)),
            patch("agent_qa.server.LOGGER.exception"),
        ):
            Handler._handle(handler, lambda: Handler._dispatch(handler))
        self.assertEqual(handler.status, 500)
        self.assertEqual(json.loads(handler.wfile.getvalue())["status"], 500)

    def test_http_send_error_uses_selected_problem_format(self):
        handler = make_handler(
            "/broken", headers={"Accept": "application/problem+json"}
        )
        Handler.send_error(handler, 400)
        body = json.loads(handler.wfile.getvalue())
        self.assertEqual(body["code"], "http_error")
        self.assertEqual(body["status"], 400)

    def test_malformed_request_target_is_a_client_error(self):
        handler = make_handler("http://[invalid")
        with self.assertRaises(ApiError) as caught:
            Handler._dispatch(handler)
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.code, "invalid_request_target")

    def test_not_found_and_method_not_allowed_precede_accept_check(self):
        handler = make_handler("/missing", headers={"Accept": "image/jpeg"})
        responses = []
        handler._json = lambda *args: responses.append(args)
        with patch("agent_qa.server.ROUTES", ()):
            Handler._dispatch(handler)
        self.assertEqual(responses[0][0], 404)

        route = {
            "path": "/known",
            "method": "GET",
            "handler": Mock(),
            "produces": ["application/json"],
        }
        handler = make_handler("/known", method="PUT", headers={"Accept": "image/jpeg"})
        responses = []
        handler._json = lambda *args: responses.append(args)
        with patch("agent_qa.server.ROUTES", (route,)):
            Handler._dispatch(handler)
        self.assertEqual(responses[0][0], 405)
        self.assertEqual(responses[0][2]["Allow"], "GET")

    def test_conditional_request_uses_canonical_etag_before_compression(self):
        canonical_etag = '"canonical"'

        def route_handler(query, path_params, payload, *, request_headers):
            headers = {"ETag": canonical_etag}
            condition = request_headers.get("If-None-Match")
            if condition and weak_match(parse_etag_list(condition), canonical_etag):
                return 304, None, headers
            return 200, {"value": "x" * 400}, headers

        route = {
            "path": "/dispatch-test",
            "method": "GET",
            "handler": route_handler,
            "conditional_headers": ["If-None-Match"],
            "rate_limited": False,
        }
        handler = make_handler(
            "/dispatch-test",
            headers={"Accept-Encoding": "gzip"},
        )

        with patch("agent_qa.server.ROUTES", (route,)):
            Handler._dispatch(handler)

        self.assertEqual(response_header(handler, "ETag"), f"W/{canonical_etag}")
        self.assertEqual(response_header(handler, "Content-Encoding"), "gzip")

        revalidated = make_handler(
            "/dispatch-test",
            headers={
                "Accept-Encoding": "gzip",
                "If-None-Match": f"W/{canonical_etag}",
            },
        )
        with patch("agent_qa.server.ROUTES", (route,)):
            Handler._dispatch(revalidated)
        self.assertEqual(revalidated.status, 304)
        self.assertEqual(response_header(revalidated, "ETag"), canonical_etag)
        self.assertEqual(revalidated.wfile.getvalue(), b"")

    def test_406_happens_before_route_handler(self):
        route_handler = Mock(return_value=(200, {"ok": True}, {}))
        route = {
            "path": "/dispatch-test",
            "method": "GET",
            "handler": route_handler,
            "produces": ["application/json"],
            "rate_limited": False,
        }
        handler = make_handler("/dispatch-test", headers={"Accept": "text/plain"})
        responses = []
        handler._json = lambda *args: responses.append(args)

        with patch("agent_qa.server.ROUTES", (route,)):
            Handler._dispatch(handler)

        route_handler.assert_not_called()
        self.assertEqual(responses[0][0], 406)
        self.assertEqual(responses[0][1]["error"]["code"], "not_acceptable")

    def test_problem_json_is_accepted_for_json_success(self):
        route_handler = Mock(return_value=(200, {"ok": True}, {}))
        route = {
            "path": "/dispatch-test",
            "method": "GET",
            "handler": route_handler,
            "produces": ["application/json"],
            "rate_limited": False,
        }
        handler = make_handler(
            "/dispatch-test", headers={"Accept": "application/problem+json"}
        )
        responses = []
        handler._json = lambda *args: responses.append(args)

        with patch("agent_qa.server.ROUTES", (route,)):
            Handler._dispatch(handler)

        route_handler.assert_called_once()
        self.assertEqual(responses[0][:2], (200, {"ok": True}))

    def test_unsupported_content_encoding_precedes_body_read(self):
        route_handler = Mock(return_value=(201, {}, {}))
        route = {
            "path": "/dispatch-test",
            "method": "POST",
            "handler": route_handler,
            "body": True,
            "auth_required": True,
            "rate_limited": False,
        }
        handler = make_handler(
            "/dispatch-test",
            method="POST",
            headers={
                "Accept": "application/problem+json",
                "Content-Encoding": "br",
                "Content-Length": "2",
            },
        )
        handler.rfile = Mock()

        with (
            patch("agent_qa.server.ROUTES", (route,)),
            patch(
                "agent_qa.server.authenticate_api_key",
                return_value={"role": "write"},
            ),
        ):
            Handler._handle(handler, lambda: Handler._dispatch(handler))

        self.assertEqual(handler.status, 415)
        self.assertEqual(
            json.loads(handler.wfile.getvalue())["code"],
            "unsupported_content_encoding",
        )
        handler.rfile.read.assert_not_called()
        route_handler.assert_not_called()

    def test_encoding_check_covers_no_body_routes_and_allows_identity(self):
        route_handler = Mock(return_value=(200, {"ok": True}, {}))
        route = {
            "path": "/dispatch-test",
            "method": "GET",
            "handler": route_handler,
            "role": "read",
            "auth_required": True,
            "rate_limited": False,
        }
        handler = make_handler("/dispatch-test", headers={})
        handler.headers.add_header("Content-Encoding", "identity")
        handler.headers.add_header("Content-Encoding", "identity")
        responses = []
        handler._json = lambda *args: responses.append(args)
        with (
            patch("agent_qa.server.ROUTES", (route,)),
            patch(
                "agent_qa.server.authenticate_api_key",
                return_value={"role": "read"},
            ) as authenticate,
        ):
            Handler._dispatch(handler)
        authenticate.assert_called_once()
        route_handler.assert_called_once()
        self.assertEqual(responses[0][0], 200)

        handler = make_handler("/dispatch-test")
        handler.headers.add_header("Content-Encoding", "identity")
        handler.headers.add_header("Content-Encoding", "br")
        with (
            patch("agent_qa.server.ROUTES", (route,)),
            patch(
                "agent_qa.server.authenticate_api_key",
                return_value={"role": "read"},
            ) as authenticate,
        ):
            with self.assertRaises(ApiError) as caught:
                Handler._dispatch(handler)
        authenticate.assert_called_once()
        self.assertEqual(caught.exception.code, "unsupported_content_encoding")

    def test_authentication_failure_precedes_content_encoding_rejection(self):
        route_handler = Mock(return_value=(200, {"ok": True}, {}))
        route = {
            "path": "/dispatch-test",
            "method": "GET",
            "handler": route_handler,
            "role": "read",
            "auth_required": True,
            "rate_limited": False,
        }
        handler = make_handler(
            "/dispatch-test",
            headers={
                "Accept": "application/problem+json",
                "Content-Encoding": "gzip",
            },
        )
        with (
            patch("agent_qa.server.ROUTES", (route,)),
            patch("agent_qa.server.authenticate_api_key", return_value=None),
        ):
            Handler._handle(handler, lambda: Handler._dispatch(handler))
        self.assertEqual(handler.status, 401)
        self.assertEqual(json.loads(handler.wfile.getvalue())["code"], "unauthorized")
        route_handler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
