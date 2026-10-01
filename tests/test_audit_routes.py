"""Unit tests for the admin audit query handlers."""

import unittest
from unittest.mock import patch

from agent_qa.context import RequestContext, clear_context, set_context
from agent_qa.errors import ApiError
from agent_qa.fulfillment import FulfillmentService
from agent_qa.orders import OrderError, OrderStore
from agent_qa.products import ProductStore
from agent_qa.routes import get_audit_entry, list_audit


class AuditRouteTests(unittest.TestCase):
    def test_list_passes_validated_filters_and_defaults(self):
        expected = {
            "method": "PATCH",
            "resource": "products",
            "resource_id": "12",
            "outcome": "success",
            "actor": "key-1",
            "status": 200,
            "since_seq": 3,
            "limit": 20,
            "order": "asc",
        }
        with patch(
            "agent_qa.routes.AUDIT_LOG.query", return_value={"items": []}
        ) as query:
            status, body, headers = list_audit(
                [(name, str(value)) for name, value in expected.items()]
            )
        self.assertEqual((status, body, headers), (200, {"items": []}, {}))
        query.assert_called_once_with(**expected)

    def test_list_rejects_unknown_duplicate_and_out_of_range_values(self):
        for query in (
            [("extra", "1")],
            [("method", "POST"), ("method", "DELETE")],
            [
                (
                    "since_seq",
                    "99999999999999999999999999999999999999999999999999999999999999999",
                )
            ],
            [("limit", "201")],
            [("status", "99")],
        ):
            with self.subTest(query=query), self.assertRaises(ApiError) as error:
                list_audit(query)
            self.assertEqual(error.exception.status, 400)
            self.assertEqual(error.exception.code, "invalid_query")

    def test_list_accepts_jobs_resource_filter(self):
        with patch(
            "agent_qa.routes.AUDIT_LOG.query", return_value={"items": []}
        ) as query:
            status, body, headers = list_audit([("resource", "jobs")])
        self.assertEqual((status, body, headers), (200, {"items": []}, {}))
        query.assert_called_once_with(
            resource="jobs", since_seq=0, limit=50, order="desc"
        )

    def test_get_entry_returns_retained_entry_and_rejects_invalid_or_missing_seq(self):
        entry = {"seq": 7}
        with patch("agent_qa.routes.AUDIT_LOG.get", return_value=entry) as get:
            self.assertEqual(get_audit_entry([], {"seq": "7"}), (200, entry, {}))
            get.assert_called_once_with(7)
        with patch("agent_qa.routes.AUDIT_LOG.get", return_value=None):
            for seq in ("0", "99999999999999999999999", "nope"):
                with self.subTest(seq=seq), self.assertRaises(ApiError) as error:
                    get_audit_entry([], {"seq": seq})
                self.assertEqual(error.exception.status, 404)
                self.assertEqual(error.exception.code, "audit_entry_not_found")

    def test_store_context_records_only_real_safe_field_changes(self):
        orders = OrderStore()
        order = orders.create("customer-1", 500)
        context = RequestContext(request_id="request-1")
        set_context(context)
        try:
            orders.update(order["id"], {"status": "paid"})
            self.assertEqual(
                context.changes,
                {"status": {"from": "new", "to": "paid"}},
            )
        finally:
            clear_context()

        products = ProductStore()
        product = products.create(
            sku="ITEM-1", name="Item", category="tools", price_cents=100
        )
        context = RequestContext(request_id="request-2")
        set_context(context)
        try:
            products.adjust_stock(product["id"], 2)
            self.assertEqual(context.changes, {"stock": {"from": 0, "to": 2}})
            products.update(product["id"], {"name": "Item"})
            self.assertIsNone(context.changes)
            products.update(product["id"], {"name": "Renamed", "price_cents": 120})
            self.assertEqual(
                context.changes,
                {
                    "name": {"from": "Item", "to": "Renamed"},
                    "price_cents": {"from": 100, "to": 120},
                },
            )
        finally:
            clear_context()

    def test_rejected_mutations_do_not_keep_change_diffs(self):
        orders = OrderStore()
        order = orders.create("customer-1", 500)
        context = RequestContext(request_id="request-3")
        set_context(context)
        try:
            orders.update(order["id"], {"status": "paid"})
            with self.assertRaises(OrderError):
                orders.update(order["id"], {"total_cents": 600})
            self.assertIsNone(context.changes)
            with self.assertRaises(OrderError):
                orders.update(order["id"], {"status": "new"})
            self.assertIsNone(context.changes)
        finally:
            clear_context()

        products = ProductStore()
        empty = products.create(
            sku="EMPTY", name="Empty", category="tools", price_cents=100
        )
        full = products.create(
            sku="FULL",
            name="Full",
            category="tools",
            price_cents=100,
            stock=1_000_000,
        )
        context = RequestContext(request_id="request-4")
        set_context(context)
        try:
            with self.assertRaises(ApiError):
                products.adjust_stock(empty["id"], -1)
            self.assertIsNone(context.changes)
            with self.assertRaises(ApiError):
                products.adjust_stock(full["id"], 1)
            self.assertIsNone(context.changes)
        finally:
            clear_context()

    def test_failed_item_order_cancellation_clears_rolled_back_diff(self):
        orders = OrderStore()
        products = ProductStore()
        product = products.create(
            sku="STOCK", name="Stock", category="tools", price_cents=100, stock=4
        )
        service = FulfillmentService(orders, products)
        order = service.create(
            customer_id="customer-1",
            items=[{"product_id": product["id"], "quantity": 1}],
        )
        context = RequestContext(request_id="request-5")
        set_context(context)
        try:
            with self.assertRaises(ApiError):
                service.update(order["id"], {"total_cents": 200})
            self.assertIsNone(context.changes)
            with patch.object(products, "release", side_effect=RuntimeError("failed")):
                with self.assertRaises(RuntimeError):
                    service.update(order["id"], {"status": "cancelled"})
            self.assertIsNone(context.changes)
            self.assertEqual(orders.get(order["id"])["status"], "new")
        finally:
            clear_context()


if __name__ == "__main__":
    unittest.main()
