import unittest

from agent_qa.audit import AuditLog
from agent_qa.context import RequestContext
from agent_qa.jobs import JobRunner, make_builtin_handlers
from agent_qa.orders import OrderStore
from agent_qa.outbox import OutboxStore
from agent_qa.products import ProductStore
from agent_qa.stats import build_stats


class StatsAggregationTests(unittest.TestCase):
    def setUp(self):
        self.orders = [
            {
                "id": 1,
                "customer_id": "b",
                "total_cents": 200,
                "status": "paid",
                "items": [
                    {
                        "product_id": 1,
                        "sku": "ITEM-1",
                        "quantity": 2,
                        "line_total_cents": 200,
                    }
                ],
            },
            {
                "id": 2,
                "customer_id": "a",
                "total_cents": 100,
                "status": "shipped",
                "items": [
                    {
                        "product_id": 3,
                        "sku": "ITEM-3",
                        "quantity": 1,
                        "line_total_cents": 20,
                    }
                ],
            },
            {
                "id": 3,
                "customer_id": "ignored",
                "total_cents": 500,
                "status": "cancelled",
                "items": [
                    {
                        "product_id": 2,
                        "sku": "ITEM-2",
                        "quantity": 5,
                        "line_total_cents": 2500,
                    }
                ],
            },
            {
                "id": 4,
                "customer_id": "a",
                "total_cents": 300,
                "status": "new",
                "items": [],
            },
        ]
        self.products = [
            {
                "id": 1,
                "sku": "ITEM-1",
                "category": "tools",
                "price_cents": 100,
                "stock": 2,
                "active": True,
            },
            {
                "id": 2,
                "sku": "ITEM-2",
                "category": "tools",
                "price_cents": 500,
                "stock": 0,
                "active": False,
            },
            {
                "id": 3,
                "sku": "ITEM-3",
                "category": "books",
                "price_cents": 20,
                "stock": 6,
                "active": True,
            },
        ]

    def test_aggregates_orders_products_requests_and_counts(self):
        metrics = {
            "requests": [
                {"route": "/orders", "status": "200", "count": 2},
                {"route": "/orders", "status": "404", "count": 1},
                {"route": "/jobs", "status": "503", "count": 3},
            ],
            "durations": [
                {"route": "/orders", "count": 2, "total_seconds": 0.5},
                {"route": "/orders", "count": 1, "total_seconds": 0.1},
                {"route": "/jobs", "count": 3, "total_seconds": 1.0},
            ],
        }
        result = build_stats(
            self.orders,
            self.products,
            metrics,
            {"queued": 1},
            {"failed": 2},
            {"entries": 4, "dropped": 3},
            top=5,
        )

        self.assertEqual(
            result["orders"],
            {
                "total": 4,
                "by_status": {"new": 1, "paid": 1, "shipped": 1, "cancelled": 1},
                "revenue_cents": 300,
                "average_total_cents": 275,
                "itemized": 3,
                "top_customers": [
                    {"customer_id": "a", "orders": 2, "spent_cents": 400},
                    {"customer_id": "b", "orders": 1, "spent_cents": 200},
                ],
            },
        )
        self.assertEqual(result["products"]["inventory_value_cents"], 320)
        self.assertEqual(result["products"]["out_of_stock"], 1)
        self.assertEqual(result["products"]["low_stock"], 1)
        self.assertEqual(
            result["products"]["by_category"],
            [
                {"category": "books", "products": 1, "inventory_value_cents": 120},
                {"category": "tools", "products": 2, "inventory_value_cents": 200},
            ],
        )
        self.assertEqual(
            result["products"]["top_products"],
            [
                {
                    "product_id": 1,
                    "sku": "ITEM-1",
                    "units_sold": 2,
                    "revenue_cents": 200,
                },
                {
                    "product_id": 3,
                    "sku": "ITEM-3",
                    "units_sold": 1,
                    "revenue_cents": 20,
                },
            ],
        )
        self.assertEqual(result["requests"]["total"], 6)
        self.assertEqual(result["requests"]["errors_4xx"], 1)
        self.assertEqual(result["requests"]["errors_5xx"], 3)
        routes = {row["route"]: row for row in result["requests"]["by_route"]}
        self.assertEqual(routes["/orders"]["avg_ms"], 200.0)
        self.assertEqual(result["jobs"]["queued"], 1)
        self.assertEqual(result["jobs"]["cancelled"], 0)
        self.assertEqual(result["outbox"]["failed"], 2)
        self.assertEqual(result["audit"], {"entries": 4, "dropped": 3})

    def test_empty_snapshot_and_top_validation_boundaries(self):
        empty = build_stats(
            [],
            [],
            {"requests": [], "durations": []},
            {},
            {},
            {"entries": 0, "dropped": 0},
        )
        self.assertEqual(empty["orders"]["average_total_cents"], 0)
        self.assertEqual(empty["orders"]["by_status"]["cancelled"], 0)
        self.assertEqual(empty["requests"]["by_route"], [])
        self.assertEqual(len(empty["jobs"]), 6)
        self.assertEqual(len(empty["outbox"]), 4)

        tied_products = [
            {
                "id": product_id,
                "sku": f"ITEM-{product_id}",
                "category": "tools",
                "price_cents": 100,
                "stock": 1,
                "active": True,
            }
            for product_id in (2, 1)
        ]
        tied_sales = [
            {
                "customer_id": f"customer-{product_id}",
                "total_cents": 100,
                "status": "paid",
                "items": [
                    {
                        "product_id": product_id,
                        "sku": f"ITEM-{product_id}",
                        "quantity": 1,
                        "line_total_cents": 100,
                    }
                ],
            }
            for product_id in (2, 1)
        ]
        product_top = build_stats(
            tied_sales,
            tied_products,
            {"requests": [], "durations": []},
            {},
            {},
            {"entries": 0, "dropped": 0},
            top=1,
        )["products"]["top_products"]
        self.assertEqual(product_top[0]["product_id"], 1)

        tied_orders = [
            {"customer_id": customer, "total_cents": 100, "status": "new", "items": []}
            for customer in ("z", "a")
        ]
        self.assertEqual(
            build_stats(
                tied_orders,
                [],
                {"requests": [], "durations": []},
                {},
                {},
                {"entries": 0, "dropped": 0},
                top=1,
            )["orders"]["top_customers"],
            [{"customer_id": "a", "orders": 1, "spent_cents": 100}],
        )
        for invalid in (0, 21, True):
            with self.subTest(top=invalid), self.assertRaises((TypeError, ValueError)):
                build_stats(
                    [],
                    [],
                    {"requests": [], "durations": []},
                    {},
                    {},
                    {"entries": 0, "dropped": 0},
                    top=invalid,
                )

    def test_store_snapshots_are_detached_and_consistent(self):
        orders = OrderStore()
        created_order = orders.create("customer-1", 123)
        order_snapshot = orders.snapshot()
        order_snapshot[0]["items"].append({"product_id": 9})
        self.assertEqual(orders.get(created_order["id"])["items"], [])

        products = ProductStore()
        created_product = products.create(
            sku="ITEM-1", name="Item", category="tools", price_cents=12, tags=["a"]
        )
        product_snapshot = products.snapshot()
        product_snapshot[0]["tags"].append("changed")
        self.assertEqual(products.get(created_product["id"])["tags"], ["a"])

        jobs = JobRunner(make_builtin_handlers(orders, products), workers=1)
        self.assertEqual(
            set(jobs.snapshot()),
            {"queued", "running", "cancelling", "succeeded", "failed", "cancelled"},
        )
        jobs.stop(timeout=1)
        outbox = OutboxStore(start_dispatcher=False)
        self.assertEqual(
            set(outbox.snapshot()), {"pending", "retrying", "delivered", "failed"}
        )
        audit = AuditLog(10)
        self.assertEqual(
            audit.snapshot(tenant="tenant-a"), {"entries": 0, "dropped": 0}
        )

    def test_audit_snapshot_counts_the_selected_tenants_evictions(self):
        audit = AuditLog(10)
        for sequence in range(10):
            audit.append(
                RequestContext(request_id=f"a-{sequence}", tenant="tenant-a"),
                "POST",
                "/orders",
                "/orders",
                201,
            )
        audit.append(
            RequestContext(request_id="b-1", tenant="tenant-b"),
            "POST",
            "/orders",
            "/orders",
            201,
        )
        self.assertEqual(audit.snapshot("tenant-a"), {"entries": 9, "dropped": 1})
        self.assertEqual(audit.snapshot("tenant-b"), {"entries": 1, "dropped": 0})


if __name__ == "__main__":
    unittest.main()
