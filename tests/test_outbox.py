import hashlib
import hmac
import json
import threading
import unittest

from agent_qa.errors import ApiError
from agent_qa.outbox import OutboxStore


class FakeClock:
    def __init__(self, value=1_700_000_000):
        self.value = value

    def __call__(self):
        return self.value


def webhook(url="https://ok.invalid/hook", **overrides):
    result = {
        "url": url,
        "events": ["order.created", "product.*"],
        "secret": "secret-123",
    }
    result.update(overrides)
    return result


class OutboxStoreTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.store = OutboxStore(clock=self.clock, start_dispatcher=False)
        self.store.create_webhook(webhook())

    def test_emit_matches_patterns_and_never_returns_secret(self):
        created = self.store.list_webhooks()["items"][0]
        self.assertTrue(created["secret_set"])
        self.assertNotIn("secret", created)

        self.store.emit("order.created", {"id": 12, "state": "new"})
        self.store.emit("order.updated", {"id": 12})
        self.store.emit("product.deleted", {"id": 4})
        result = self.store.list_outbox([])
        self.assertEqual(result["total"], 2)
        self.assertEqual(
            [item["event_type"] for item in result["items"]],
            [
                "order.created",
                "product.deleted",
            ],
        )
        self.assertEqual(result["items"][0]["payload"]["data"]["id"], 12)

    def test_signature_vector_and_successful_delivery(self):
        self.store.emit("order.created", {"id": 12})
        entry = self.store.list_outbox([])["items"][0]
        result = self.store.process_due(now=self.clock.value)
        self.assertEqual(
            result,
            {"processed": 1, "delivered": 1, "retrying": 0, "failed": 0},
        )
        attempt = self.store.get_outbox(entry["id"])["attempts"][0]
        body = json.dumps(entry["payload"], sort_keys=True, separators=(",", ":"))
        digest = hmac.new(
            b"secret-123", f"{self.clock.value}.{body}".encode(), hashlib.sha256
        ).hexdigest()
        self.assertEqual(attempt["signature"], f"sha256={digest}")
        self.assertEqual(attempt["timestamp"], self.clock.value)
        self.assertEqual(self.store.get_outbox(entry["id"])["status"], "delivered")

    def test_retry_backoff_and_attempt_budget(self):
        self.store.create_webhook(
            webhook("https://fail.invalid/hook", max_attempts=3, backoff_base_ms=1000)
        )
        self.store.emit("order.created", {"id": 1})
        entry_id = self.store.list_outbox([])["items"][-1]["id"]
        self.store.process_due(now=self.clock.value)
        current = self.store.get_outbox(entry_id)
        self.assertEqual(current["status"], "retrying")
        self.assertEqual(current["attempts"][-1]["outcome"], "http_500")
        self.assertEqual(current["next_attempt_at"], "2023-11-14T22:13:21Z")

        self.clock.value += 0.999
        self.assertEqual(self.store.process_due(now=self.clock.value)["processed"], 0)
        self.clock.value += 0.001
        self.store.process_due(now=self.clock.value)
        self.assertEqual(
            self.store.get_outbox(entry_id)["next_attempt_at"],
            "2023-11-14T22:13:23Z",
        )
        self.clock.value += 2
        self.store.process_due(now=self.clock.value)
        self.assertEqual(self.store.get_outbox(entry_id)["status"], "failed")

    def test_non_retryable_gone_fails_immediately(self):
        self.store.create_webhook(webhook("https://gone.invalid/hook"))
        self.store.emit("order.created", {"id": 1})
        item = self.store.list_outbox([])["items"][-1]
        self.store.process_due(now=self.clock.value)
        current = self.store.get_outbox(item["id"])
        self.assertEqual(current["status"], "failed")
        self.assertEqual(current["attempts"][-1]["http_status"], 410)
        self.assertIsNone(current["next_attempt_at"])

    def test_flaky_succeeds_after_two_attempts_and_requeue_resets_budget(self):
        self.store.create_webhook(
            webhook("https://flaky.invalid/hook", max_attempts=2, backoff_base_ms=0)
        )
        self.store.emit("order.created", {"id": 1})
        item = self.store.list_outbox([])["items"][-1]
        self.store.process_due(now=self.clock.value)
        self.clock.value += 1
        self.store.process_due(now=self.clock.value)
        self.assertEqual(self.store.get_outbox(item["id"])["status"], "failed")
        self.store.requeue(item["id"])
        self.clock.value += 1
        self.store.process_due(now=self.clock.value)
        final = self.store.get_outbox(item["id"])
        self.assertEqual(final["status"], "delivered")
        self.assertEqual([attempt["n"] for attempt in final["attempts"]], [1, 2, 1])

    def test_capacity_evicts_oldest_terminal_then_counts_dropped(self):
        store = OutboxStore(clock=self.clock, capacity=1, start_dispatcher=False)
        store.create_webhook(webhook())
        store.emit("order.created", {"id": 1})
        first_id = store.list_outbox([])["items"][0]["id"]
        store.process_due(now=self.clock.value)
        store.emit("order.created", {"id": 2})
        self.assertEqual(store.list_outbox([])["items"][0]["payload"]["data"]["id"], 2)
        store.emit("order.created", {"id": 3})
        self.assertEqual(
            store.metrics_snapshot(),
            ({"pending": 1, "delivered": 0, "retrying": 0, "failed": 0}, 1),
        )
        with self.assertRaises(ApiError):
            store.get_outbox(first_id)

    def test_webhook_validation_and_requeue_conflict(self):
        with self.assertRaises(ApiError) as error:
            self.store.create_webhook(webhook("https://example.com/hook"))
        self.assertEqual(error.exception.status, 400)
        self.store.emit("order.created", {"id": 1})
        item = self.store.list_outbox([])["items"][0]
        with self.assertRaises(ApiError) as error:
            self.store.requeue(item["id"])
        self.assertEqual(error.exception.code, "not_requeueable")

    def test_rejects_secret_that_cannot_be_encoded_as_utf8(self):
        with self.assertRaises(ApiError) as error:
            self.store.create_webhook(webhook(secret="\ud800" * 8))
        self.assertEqual(error.exception.status, 400)
        self.assertEqual(error.exception.details[0]["field"], "secret")

    def test_accepts_valid_http_url_fragments(self):
        created = self.store.create_webhook(
            webhook("https://hooks.invalid/path#delivery")
        )
        self.assertEqual(created["url"], "https://hooks.invalid/path#delivery")

    def test_rejects_malformed_hostname_and_unknown_webhook_fields(self):
        with self.assertRaises(ApiError):
            self.store.create_webhook(webhook("https://hooks..invalid/hook"))
        with self.assertRaises(ApiError):
            self.store.create_webhook(webhook(extra="ignored"))

    def test_slow_timeout_retries_then_fails_at_attempt_limit(self):
        self.store.create_webhook(
            webhook("https://slow.invalid/hook", max_attempts=2, backoff_base_ms=0)
        )
        self.store.emit("order.created", {"id": 1})
        item = self.store.list_outbox([])["items"][-1]

        self.store.process_due(now=self.clock.value)
        first = self.store.get_outbox(item["id"])
        self.assertEqual(first["status"], "retrying")
        self.assertEqual(first["attempts"][-1]["outcome"], "timeout")
        self.assertIsNone(first["attempts"][-1]["http_status"])

        self.store.process_due(now=self.clock.value)
        final = self.store.get_outbox(item["id"])
        self.assertEqual(final["status"], "failed")
        self.assertEqual(len(final["attempts"]), 2)

    def test_backoff_is_capped_at_one_minute(self):
        self.store.create_webhook(
            webhook("https://fail.invalid/hook", max_attempts=4, backoff_base_ms=60000)
        )
        self.store.emit("order.created", {"id": 1})
        item = self.store.list_outbox([])["items"][-1]

        self.store.process_due(now=self.clock.value)
        self.assertEqual(
            self.store.get_outbox(item["id"])["next_attempt_at"],
            "2023-11-14T22:14:20Z",
        )
        self.clock.value += 60
        self.store.process_due(now=self.clock.value)
        self.assertEqual(
            self.store.get_outbox(item["id"])["next_attempt_at"],
            "2023-11-14T22:15:20Z",
        )

    def test_delete_marks_pending_entries_failed(self):
        self.store.emit("order.created", {"id": 1})
        item = self.store.list_outbox([])["items"][0]
        self.store.delete_webhook(1)
        result = self.store.get_outbox(item["id"])
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["next_attempt_at"])

    def test_webhook_limit_is_twenty(self):
        store = OutboxStore(clock=self.clock, start_dispatcher=False)
        for index in range(20):
            store.create_webhook(webhook(f"https://hook{index}.invalid/hook"))
        with self.assertRaises(ApiError) as error:
            store.create_webhook(webhook("https://extra.invalid/hook"))
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(error.exception.code, "webhook_limit")

    def test_dispatcher_is_lazy_singleton_and_can_be_disabled_and_stopped(self):
        store = OutboxStore(clock=self.clock)
        self.assertIsNone(store._dispatcher_thread)
        store.create_webhook(webhook())
        first_thread = store._dispatcher_thread
        self.assertIsNotNone(first_thread)
        self.assertTrue(first_thread.daemon)

        store.create_webhook(webhook("https://other.invalid/hook"))
        self.assertIs(store._dispatcher_thread, first_thread)
        store.configure_dispatcher({"enabled": False})
        store.stop(timeout=1)
        self.assertFalse(first_thread.is_alive())

    def test_concurrent_processing_rechecks_updated_schedule(self):
        store = OutboxStore(clock=self.clock, start_dispatcher=False)
        store.create_webhook(
            webhook("https://fail.invalid/hook", backoff_base_ms=60000)
        )
        store.emit("order.created", {"id": 1})

        snapshot_barrier = threading.Barrier(2)
        inner_lock = store._lock

        class SnapshotBarrierLock:
            def __enter__(self):
                inner_lock.acquire()
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                inner_lock.release()
                if threading.current_thread().name.startswith("outbox-race-"):
                    snapshot_barrier.wait(timeout=2)

        store._lock = SnapshotBarrierLock()
        results = []
        workers = [
            threading.Thread(
                name=f"outbox-race-{index}",
                target=lambda: results.append(store.process_due(now=self.clock.value)),
            )
            for index in range(2)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=3)

        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(sum(result["processed"] for result in results), 1)
        self.assertEqual(len(store.list_outbox([])["items"][0]["attempts"]), 1)


if __name__ == "__main__":
    unittest.main()
