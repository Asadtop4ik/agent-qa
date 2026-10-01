"""Unit tests for HTTP-independent tracing primitives."""

import unittest
from unittest.mock import patch

from agent_qa.context import RequestContext, clear_context, set_context
from agent_qa.tracing import Trace, TraceBuffer, parse_traceparent, span


class ParseTraceparentTests(unittest.TestCase):
    def test_strict_version_00_cases(self):
        valid = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
        self.assertEqual(
            parse_traceparent(valid),
            ("0123456789abcdef0123456789abcdef", "0123456789abcdef", "01"),
        )
        invalid = (
            None,
            "",
            "00-0123456789abcdef0123456789abcdef-0123456789abcdef",
            valid + "-00",
            "01-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
            "00-0123456789abcdef0123456789abcdef-0123456789abcdef-0A",
            "00-0123456789abcdef0123456789abcdef-0123456789abcdeG-01",
            "00-0123456789abcdef0123456789abcdef-0123456789abcde-01",
            "00-0123456789abcdef0123456789abcde-0123456789abcdef-01",
            "00-00000000000000000000000000000000-0123456789abcdef-01",
            "00-0123456789abcdef0123456789abcdef-0000000000000000-01",
            "00-0123456789abcdef0123456789abcdef-0123456789abcdef-0",
            "00-0123456789abcdef0123456789abcdef-0123456789abcdef-010",
            "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01 ",
            " 00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
            "00-0123456789ABCDEF0123456789abcdef-0123456789abcdef-01",
            "00-0123456789abcdef0123456789abcdef-0123456789ABCDEF-01",
            "ff-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
        )
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(parse_traceparent(value))


class TraceTests(unittest.TestCase):
    def test_timing_aggregates_repeated_names_in_first_encounter_order(self):
        with patch("agent_qa.tracing.time.monotonic", side_effect=range(8)):
            trace = Trace()
            with trace.span("store"):
                pass
            with trace.span("validate"):
                pass
            with trace.span("store"):
                pass
            trace.finish(request_id="r", method="GET", route="/orders", status=200)
        self.assertEqual(
            trace.server_timing(),
            "http.request;dur=7000.000, store;dur=2000.000, "
            "validate;dur=1000.000, total;dur=7000.000",
        )

    def test_nested_spans_parent_indexes_names_and_safe_attributes(self):
        trace = Trace()
        with trace.span("route_match", route="/orders/{id}", secret="do-not-store"):
            with trace.span(
                "not-a-real-phase", route="/private/customer-secret", errors=True
            ):
                pass
        record = trace.finish(
            request_id="request-1", method="GET", route="/orders/{id}", status=200
        )
        self.assertEqual(
            [item["name"] for item in record["spans"]],
            ["http.request", "route_match", "other"],
        )
        self.assertEqual([item["parent"] for item in record["spans"]], [None, 0, 1])
        self.assertEqual(record["spans"][1]["attrs"], {"route": "/orders/{id}"})
        self.assertEqual(record["spans"][2]["attrs"], {})
        self.assertNotIn("do-not-store", repr(record))
        self.assertNotIn("customer-secret", repr(record))

    def test_unknown_operation_and_untrusted_route_attributes_are_dropped(self):
        trace = Trace()
        with trace.span(
            "store",
            op="secret_table_lookup",
            store_op="credential_value",
            route="/customer/alice",
            errors=10001,
        ):
            pass
        record = trace.finish(
            request_id="r", method="GET", route="unmatched", status=404
        )
        self.assertEqual(record["spans"][1]["attrs"], {})

    def test_traceparent_propagates_ids_and_response_headers_are_bounded(self):
        incoming = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-00"
        trace = Trace(incoming)
        self.assertEqual(
            trace.traceparent,
            "00-0123456789abcdef0123456789abcdef-" + trace.span_id + "-01",
        )
        with trace.span("validate"):
            pass
        timing = trace.server_timing()
        self.assertRegex(
            timing,
            r"^http\.request;dur=\d+\.\d{3}, validate;dur=\d+\.\d{3}, "
            r"total;dur=\d+\.\d{3}$",
        )

    def test_active_spans_are_closed_at_finish_once(self):
        trace = Trace()
        phase = trace.span("handler")
        phase.__enter__()
        record = trace.finish(request_id="r", method="GET", route="/ready", status=200)
        phase.__exit__(None, None, None)
        self.assertGreaterEqual(record["spans"][1]["duration_ms"], 0)
        self.assertEqual(
            trace.finish(request_id="r", method="GET", route="/ready", status=200),
            record,
        )

    def test_span_count_and_depth_are_bounded(self):
        trace = Trace()
        contexts = []
        for _ in range(30):
            context = trace.span("validate")
            context.__enter__()
            contexts.append(context)
        for context in reversed(contexts):
            context.__exit__(None, None, None)
        record = trace.finish(request_id="r", method="GET", route="/ready", status=200)
        self.assertLessEqual(len(record["spans"]), 16)
        trace = Trace()
        for _ in range(100):
            with trace.span("store"):
                pass
        record = trace.finish(request_id="r", method="GET", route="/orders", status=200)
        self.assertEqual(len(record["spans"]), 64)

    def test_module_span_uses_thread_local_request_context(self):
        trace = Trace()
        set_context(RequestContext("context-request", trace=trace))
        try:
            with span("auth"):
                with span("store", op="get_order"):
                    pass
        finally:
            clear_context()
        record = trace.finish(
            request_id="context-request", method="GET", route="/orders/{id}", status=200
        )
        self.assertEqual(
            [item["name"] for item in record["spans"]],
            ["http.request", "auth", "store"],
        )
        self.assertEqual(record["spans"][2]["parent"], 1)
        with span("auth"):
            pass


class TraceBufferTests(unittest.TestCase):
    def test_ring_buffer_filters_newest_and_does_not_evict_for_admin(self):
        buffer = TraceBuffer(10)
        for index in range(12):
            buffer.append(
                {
                    "trace_id": f"{index:032x}",
                    "route": "/orders",
                    "status": 200,
                    "duration_ms": float(index),
                    "index": index,
                }
            )
        before = buffer.list()
        buffer.append({"trace_id": "f" * 32, "route": "/admin/traces", "status": 200})
        self.assertEqual(buffer.list(), before)
        filtered = buffer.list(route="/orders", status=200, min_duration_ms=10, limit=1)
        self.assertEqual(filtered["total_matching"], 2)
        self.assertEqual(filtered["items"][0]["index"], 11)
        self.assertEqual(filtered["capacity"], 10)
        self.assertIsNone(buffer.get("bad-id"))
        self.assertEqual(buffer.get("0000000000000000000000000000000b")["index"], 11)

    def test_capacity_and_query_bounds(self):
        for capacity in (0, 9, 1001, True):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                TraceBuffer(capacity)
        buffer = TraceBuffer(10)
        invalid_queries = (
            {"limit": 0},
            {"limit": 101},
            {"status": True},
            {"min_duration_ms": float("inf")},
            {"min_duration_ms": 10**10000},
        )
        for kwargs in invalid_queries:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                buffer.list(**kwargs)


if __name__ == "__main__":
    unittest.main()
