"""Tests for the in-memory audit ring buffer."""

import os
import threading
import unittest
from unittest.mock import patch

from agent_qa.audit import AuditLog
from agent_qa.context import RequestContext, clear_context, set_context


class AuditLogTests(unittest.TestCase):
    def test_capacity_configuration_uses_registry_defaults_for_invalid_values(self):
        for value, expected in (("10", 10), ("5000", 5000), ("5001", 500)):
            with self.subTest(value=value), patch.dict(
                os.environ, {"AGENT_QA_AUDIT_CAPACITY": value}
            ):
                self.assertEqual(AuditLog().capacity, expected)
        for value in ("", "nope", "9" * 10000):
            with self.subTest(value=value[:10]), patch.dict(
                os.environ, {"AGENT_QA_AUDIT_CAPACITY": value}
            ):
                self.assertEqual(AuditLog().capacity, 500)
        with patch.dict(os.environ, {"AGENT_QA_AUDIT_CAPACITY": "0"}):
            self.assertEqual(AuditLog().capacity, 500)

    def test_ring_buffer_drops_oldest_and_never_reuses_sequence(self):
        log = AuditLog(10)
        context = RequestContext("request")
        for _ in range(13):
            log.append(context, "POST", "/orders", "/orders", 201)
        result = log.query(order="asc")
        self.assertEqual([item["seq"] for item in result["items"]], list(range(4, 14)))
        self.assertEqual(result["dropped"], 3)
        self.assertEqual(result["last_seq"], 13)
        self.assertEqual(result["capacity"], 10)
        self.assertIsNone(log.get(3))
        self.assertEqual(
            log.append(context, "POST", "/orders", "/orders", 201)["seq"], 14
        )

    def test_concurrent_appends_assign_unique_monotonic_sequences(self):
        log = AuditLog(1000)
        context = RequestContext("threaded")
        barrier = threading.Barrier(8)
        failures = []

        def append_many():
            try:
                barrier.wait(timeout=3)
                for _ in range(100):
                    log.append(context, "POST", "/orders", "/orders", 201)
            except Exception as error:  # surfaced in the test thread
                failures.append(error)

        workers = [threading.Thread(target=append_many) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(failures, [])
        seqs = [item["seq"] for item in log.query(limit=200, order="asc")["items"]]
        self.assertEqual(seqs, list(range(1, 201)))
        self.assertEqual(log.last_seq, 800)
        self.assertEqual(log.dropped, 0)

    def test_filters_since_order_and_total_are_consistent(self):
        log = AuditLog(10)
        for resource_id, (method, resource, status) in enumerate(
            (
                ("POST", "orders", 201),
                ("PATCH", "orders", 409),
                ("DELETE", "products", 204),
            ),
            start=1,
        ):
            log.append(
                RequestContext(
                    "actor",
                    actor="admin-key",
                    role="admin",
                    resource=resource,
                    resource_id=resource_id,
                ),
                method,
                f"/{resource}",
                f"/{resource}",
                status,
            )
        page = log.query(method="PATCH", resource="orders", since_seq=1)
        self.assertEqual(page["total_matching"], 1)
        self.assertEqual([item["seq"] for item in page["items"]], [2])
        self.assertEqual(page["items"][0]["outcome"], "rejected")
        asc = log.query(since_seq=1, order="asc", limit=1)
        self.assertEqual(asc["items"][0]["seq"], 2)
        self.assertEqual(asc["total_matching"], 2)
        self.assertEqual(log.query(resource_id="missing")["items"], [])
        self.assertEqual(log.query(resource_id="2")["items"][0]["seq"], 2)
        self.assertEqual(log.query(actor="admin-key")["total_matching"], 3)
        self.assertEqual(log.query(actor="missing")["total_matching"], 0)
        self.assertEqual(log.query(outcome="success")["total_matching"], 2)
        self.assertEqual(log.query(status=409)["items"][0]["seq"], 2)

    def test_maintenance_503_can_be_audited_as_rejected(self):
        log = AuditLog(10)
        context = RequestContext("maintenance", tenant="acme")
        ordinary = log.append(context, "POST", "/orders", "/orders", 503)
        maintenance = log.append(
            context, "POST", "/orders", "/orders", 503, rejected=True
        )
        self.assertEqual(ordinary["outcome"], "error")
        self.assertEqual(maintenance["outcome"], "rejected")

    def test_changes_are_detached_bounded_and_sensitive_fields_omitted(self):
        log = AuditLog(10)
        tags_before = ["old"]
        tags_after = ["new"]
        changes = {
            "status": {"from": "new", "to": "paid"},
            "tags": {"from": tags_before, "to": tags_after},
            "active": {"from": True, "to": True},
            "api_key": {"from": "secret-before", "to": "secret-after"},
        }
        context = RequestContext("change", changes=changes)
        entry = log.append(context, "PATCH", "/orders/{id}", "/orders/4", 200)
        changes["status"]["to"] = "cancelled"
        tags_after.append("later")
        expected = {
            "status": {"from": "new", "to": "paid"},
            "tags": {"from": ["old"], "to": ["new"]},
        }
        self.assertEqual(entry["changes"], expected)
        self.assertNotIn("secret-before", repr(log.get(1)))
        self.assertEqual(log.get(1)["resource_id"], None)
        returned = log.get(1)
        returned["changes"]["tags"]["to"].append("mutated")
        query = log.query()["items"][0]
        query["changes"]["status"]["to"] = "mutated"
        self.assertEqual(log.get(1)["changes"], expected)

    def test_long_queryless_path_is_preserved_and_actor_is_bounded(self):
        log = AuditLog(10)
        path = "/orders/" + "x" * 4000
        entry = log.append(
            RequestContext("request", actor="a" * 200),
            "POST",
            "/orders/{id}",
            path + "?secret=query",
            201,
        )
        self.assertEqual(entry["path"], path)
        self.assertEqual(len(entry["actor"]), 128)

    def test_invalid_query_bounds_are_rejected(self):
        log = AuditLog(10)
        for params in (
            {"limit": 201},
            {"since_seq": -1},
            {"status": 10**100},
            {"order": "random"},
        ):
            with self.subTest(params=params), self.assertRaises(ValueError):
                log.query(**params)

    def test_audit_entries_are_filtered_by_request_tenant(self):
        log = AuditLog(10)
        log.append(RequestContext("default-write"), "POST", "/orders", "/orders", 201)
        log.append(
            RequestContext("tenant-write", tenant="acme"),
            "POST",
            "/orders",
            "/orders",
            201,
        )
        self.assertEqual(log.query()["total_matching"], 1)
        self.assertEqual(log.query(tenant="acme")["items"][0]["tenant"], "acme")
        self.assertIsNone(log.get(2))
        self.assertEqual(log.get(2, tenant="acme")["tenant"], "acme")
        self.assertEqual(log.query(tenant="*")["total_matching"], 2)
        set_context(RequestContext("tenant-read", tenant="acme"))
        try:
            self.assertEqual(log.query()["items"][0]["tenant"], "acme")
        finally:
            clear_context()


if __name__ == "__main__":
    unittest.main()
