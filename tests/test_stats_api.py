"""HTTP metadata and validation tests for administrator statistics."""

import unittest
from unittest.mock import patch

from agent_qa.audit import AuditLog
from agent_qa.context import RequestContext, clear_context, set_context
from agent_qa.errors import ApiError
from agent_qa.metrics import MetricsRegistry
from agent_qa.openapi import build_openapi
from agent_qa.routes import ROUTES, admin_stats
from agent_qa.tenants import TenantRegistry


class StatsApiTests(unittest.TestCase):
    def tearDown(self):
        clear_context()

    def test_sections_select_output_and_default_has_all_aggregates(self):
        set_context(RequestContext(request_id="stats-test", tenant="default"))
        status, selected, _ = admin_stats([("top", "1"), ("sections", "orders")])
        self.assertEqual(status, 200)
        self.assertEqual(
            set(selected), {"generated_at", "tenant", "uptime_seconds", "orders"}
        )
        self.assertEqual(selected["tenant"], "default")
        self.assertIsInstance(selected["uptime_seconds"], int)

        _, complete, _ = admin_stats([])
        self.assertEqual(
            set(complete),
            {
                "generated_at",
                "tenant",
                "uptime_seconds",
                "orders",
                "products",
                "requests",
                "jobs",
                "outbox",
                "audit",
            },
        )

    def test_query_values_are_bounded_and_sections_errors_name_field(self):
        for query, field in (
            ([("top", "999999999999999999999999999999")], "top"),
            ([("top", "0")], "top"),
            ([("sections", "orders,unknown")], "sections"),
            ([("sections", ",orders")], "sections"),
        ):
            with self.subTest(query=query), self.assertRaises(ApiError) as error:
                admin_stats(query)
            self.assertEqual(error.exception.status, 400)
            self.assertEqual(error.exception.details[0]["field"], field)

    def test_selected_tenant_is_isolated_and_requests_are_global(self):
        registry = TenantRegistry()
        metrics = MetricsRegistry()
        audit = AuditLog(10)
        first = registry.get("tenant-a")
        second = registry.get("tenant-b")
        first.orders.create("first", 100)
        second.orders.create("second", 900)
        metrics.record("GET", "/orders", 200, 0.01)
        audit.append(
            RequestContext("audit-a", tenant="tenant-a"),
            "POST",
            "/orders",
            "/orders",
            201,
        )
        with (
            patch(
                "agent_qa.routes.tenants.get",
                side_effect={"tenant-a": first, "tenant-b": second}.__getitem__,
            ),
            patch("agent_qa.routes.REGISTRY", metrics),
            patch("agent_qa.routes.AUDIT_LOG", audit),
        ):
            set_context(RequestContext("stats-a", tenant="tenant-a"))
            _, selected, _ = admin_stats([])
            set_context(RequestContext("stats-b", tenant="tenant-b"))
            _, other, _ = admin_stats([])
        self.assertEqual(selected["orders"]["average_total_cents"], 100)
        self.assertEqual(other["orders"]["average_total_cents"], 900)
        self.assertEqual(selected["audit"]["entries"], 1)
        self.assertEqual(other["audit"]["entries"], 0)
        self.assertEqual(selected["requests"], other["requests"])
        self.assertEqual(selected["requests"]["total"], 1)

    def test_routes_and_openapi_document_admin_tenant_scope_and_parameters(self):
        route = next(route for route in ROUTES if route["path"] == "/admin/stats")
        self.assertEqual(route["role"], "admin")
        self.assertTrue(route["tenant_scoped"])
        spec = build_openapi(ROUTES, "stats-test")
        operation = spec["paths"]["/admin/stats"]["get"]
        parameters = {item["name"]: item for item in operation["parameters"]}
        self.assertEqual(parameters["top"]["schema"]["maximum"], 20)
        self.assertEqual(parameters["sections"]["schema"]["maxLength"], 128)
        self.assertIn(
            "orders, products, requests, jobs, outbox, audit",
            parameters["sections"]["description"],
        )
        self.assertEqual(operation["x-required-role"], "admin")
        self.assertIn("X-Tenant", parameters)
        response_schema = operation["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        self.assertEqual(
            set(response_schema["properties"]),
            {
                "generated_at",
                "tenant",
                "uptime_seconds",
                "orders",
                "products",
                "requests",
                "jobs",
                "outbox",
                "audit",
            },
        )
        self.assertEqual(
            set(response_schema["properties"]["orders"]["properties"]),
            {
                "total",
                "by_status",
                "revenue_cents",
                "average_total_cents",
                "itemized",
                "top_customers",
            },
        )


if __name__ == "__main__":
    unittest.main()
