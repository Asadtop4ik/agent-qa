"""Tests for tenant registry lifecycle and resource isolation."""

import threading
import time
import unittest
from unittest.mock import patch

from agent_qa.audit import AuditLog
from agent_qa.context import RequestContext, clear_context, set_context
from agent_qa.errors import ApiError
from agent_qa.idempotency import StoredResponse
from agent_qa.tenants import MAX_TENANTS, TenantRegistry, current, validate_name


class TenantRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = TenantRegistry()

    def tearDown(self):
        clear_context()
        for tenant in self.registry.tenant_names():
            if tenant != "default":
                self.registry.delete(tenant)

    def test_names_default_lazy_reads_and_bundle_isolation(self):
        self.assertEqual(validate_name("team-1"), "team-1")
        for name in ("", "-team", "Team", "a" * 25, "team_name"):
            with self.subTest(name=name), self.assertRaises(ApiError) as error:
                validate_name(name)
            self.assertEqual(error.exception.code, "invalid_tenant")

        read_only = self.registry.get("not-yet-created")
        self.assertEqual(read_only.orders.list(limit=10)[1], 0)
        self.assertEqual(self.registry.tenant_names(), ("default",))
        first = self.registry.ensure("alpha")
        second = self.registry.ensure("beta")
        self.assertEqual(first.orders.create("a", 100)["id"], 1)
        self.assertEqual(second.orders.create("b", 200)["id"], 1)
        first.products.create(
            sku="SAME",
            name="Alpha",
            category="test",
            price_cents=1,
            stock=2,
            tags=[],
            active=True,
        )
        second.products.create(
            sku="SAME",
            name="Beta",
            category="test",
            price_cents=1,
            stock=2,
            tags=[],
            active=True,
        )
        self.assertEqual(first.products.list(limit=10)[1], 1)
        self.assertEqual(second.products.list(limit=10)[1], 1)
        self.assertNotEqual(first.orders, second.orders)
        self.assertNotEqual(first.outbox, second.outbox)
        self.assertNotEqual(first.jobs, second.jobs)
        self.assertNotEqual(first.idempotency, second.idempotency)

        first.orders.create("a-2", 101)
        _rows, _total, cursor = first.orders.list(limit=1, pagination="cursor")
        with self.assertRaises(ApiError) as error:
            second.orders.list(limit=1, pagination="cursor", cursor=cursor)
        self.assertEqual(error.exception.code, "cursor_mismatch")

        first.outbox.create_webhook(
            {
                "url": "https://hooks.example.invalid/hook",
                "events": ["order.created"],
                "secret": "test-secret",
            }
        )
        first.outbox.emit("order.created", {"id": 1})
        self.assertEqual(first.outbox.list_webhooks()["total"], 1)
        self.assertEqual(second.outbox.list_webhooks()["total"], 0)

        job = first.jobs.submit("fail", {"message": "expected"})
        first.jobs.get(job["id"], wait_ms=1000)
        self.assertEqual(first.jobs._jobs[job["id"]]["tenant"], "alpha")
        self.assertIsNone(second.jobs.get(job["id"]))

        scope = ("POST", "/orders", "same-key")
        response = StoredResponse(201, {"id": 1}, {})
        first.idempotency.begin(scope, "one")
        first.idempotency.complete(scope, response)
        self.assertEqual(second.idempotency.begin(scope, "two").kind, "new")

    def test_current_context_uses_selected_tenant(self):
        alpha = self.registry.ensure("alpha")
        set_context(RequestContext("request", tenant="alpha"))
        with patch("agent_qa.tenants.TENANTS", self.registry):
            self.assertIs(current(), alpha)

    def test_limit_and_delete_purge_preserve_audit(self):
        for index in range(1, MAX_TENANTS):
            self.registry.ensure(f"tenant-{index}")
        with self.assertRaises(ApiError) as error:
            self.registry.ensure("extra")
        self.assertEqual(error.exception.code, "tenant_limit")
        with self.assertRaises(ApiError) as error:
            self.registry.delete("default")
        self.assertEqual(error.exception.code, "default_tenant_protected")
        with self.assertRaises(ApiError) as error:
            self.registry.delete("missing")
        self.assertEqual(error.exception.code, "tenant_not_found")

        registry = TenantRegistry()
        tenant = registry.ensure("purge-me")
        tenant.orders.create("customer", 10)
        tenant.products.create(
            sku="PURGE-1",
            name="Purge test",
            category="test",
            price_cents=10,
            stock=2,
            tags=[],
            active=True,
        )
        tenant.outbox.create_webhook(
            {
                "url": "https://hooks.example.invalid/hook",
                "events": ["order.created"],
                "secret": "purge-secret",
            }
        )
        tenant.outbox.emit("order.created", {"id": 1})
        job = tenant.jobs.submit("sleep", {"duration_ms": 5000})
        deadline = time.monotonic() + 1
        while tenant.jobs.get(job["id"])["status"] != "running":
            if time.monotonic() >= deadline:
                self.fail("sleep job did not start")
            time.sleep(0.001)
        cancel_event = tenant.jobs._cancel_events[job["id"]]
        scope = ("POST", "/orders", "purge-key")
        response = StoredResponse(201, {"id": 1}, {})
        tenant.idempotency.begin(scope, "purge-fingerprint")
        tenant.idempotency.complete(scope, response)
        log = AuditLog(10)
        log.append(
            RequestContext("audit", tenant="purge-me"),
            "POST",
            "/orders",
            "/orders",
            201,
        )
        registry.delete("purge-me")
        self.assertTrue(cancel_event.is_set())
        self.assertEqual(tenant.orders.list(limit=1)[1], 0)
        self.assertEqual(tenant.products.list(limit=1)[1], 0)
        self.assertEqual(tenant.jobs.list_jobs()["total"], 0)
        self.assertEqual(tenant.outbox.list_webhooks()["total"], 0)
        self.assertEqual(tenant.outbox.list_outbox([])["total"], 0)
        self.assertEqual(
            tenant.idempotency.begin(scope, "purge-fingerprint").kind, "new"
        )
        self.assertEqual(registry.get("purge-me").orders.list(limit=1)[1], 0)
        self.assertEqual(log.query(tenant="purge-me")["total_matching"], 1)

    def test_shutdown_stops_workers_without_purging_resources(self):
        tenant = self.registry.ensure("shutdown")
        tenant.orders.create("customer", 10)
        tenant.outbox.create_webhook(
            {
                "url": "https://hooks.example.invalid/hook",
                "events": ["order.created"],
                "secret": "shutdown-secret",
            }
        )
        tenant.outbox.emit("order.created", {"id": 1})
        tenant.jobs.submit("sleep", {"duration_ms": 5000})

        self.registry.shutdown(timeout=1)

        self.assertEqual(tenant.orders.list(limit=1)[1], 1)
        self.assertEqual(tenant.outbox.list_webhooks()["total"], 1)
        self.assertEqual(tenant.outbox.list_outbox([])["total"], 1)
        self.assertEqual(tenant.jobs.list_jobs()["total"], 1)

    def test_two_tenant_concurrent_writes_keep_independent_ids(self):
        tenants = (self.registry.ensure("race-a"), self.registry.ensure("race-b"))
        barrier = threading.Barrier(2)
        failures = []

        def create_many(bundle, customer):
            try:
                barrier.wait(timeout=3)
                for index in range(50):
                    bundle.orders.create(f"{customer}-{index}", index)
            except Exception as error:  # surfaced in the test thread
                failures.append(error)

        workers = [
            threading.Thread(target=create_many, args=(tenants[0], "a")),
            threading.Thread(target=create_many, args=(tenants[1], "b")),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(failures, [])
        for bundle in tenants:
            rows, total, _cursor = bundle.orders.list(limit=100, pagination="cursor")
            self.assertEqual(total, 50)
            self.assertEqual([row["id"] for row in rows], list(range(1, 51)))


if __name__ == "__main__":
    unittest.main()
