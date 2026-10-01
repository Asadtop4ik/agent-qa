"""Unit tests for bounded token-bucket rate limiting."""

import threading
import unittest

from agent_qa.errors import ApiError
from agent_qa.ratelimit import TokenBucketLimiter


class TokenBucketLimiterTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.limiter = TokenBucketLimiter(3, 0.5, self.clock)

    def clock(self):
        return self.now

    def test_burst_and_fractional_refill(self):
        self.assertTrue(self.limiter.consume("ip:a").allowed)
        self.assertTrue(self.limiter.consume("ip:a").allowed)
        third = self.limiter.consume("ip:a")
        self.assertTrue(third.allowed)
        self.assertEqual(third.headers["RateLimit-Remaining"], "0")
        self.assertFalse(self.limiter.consume("ip:a").allowed)
        self.now += 1.9
        self.assertFalse(self.limiter.consume("ip:a").allowed)
        self.now += 0.1
        self.assertTrue(self.limiter.consume("ip:a").allowed)

    def test_retry_after_and_reset_round_up(self):
        limiter = TokenBucketLimiter(1, 0.4, self.clock)
        self.assertTrue(limiter.consume("client:a").allowed)
        denied = limiter.consume("client:a")
        self.assertEqual(denied.headers["RateLimit-Remaining"], "0")
        self.assertEqual(denied.headers["Retry-After"], "3")
        self.assertEqual(denied.headers["RateLimit-Reset"], "3")

    def test_saturated_table_retry_tracks_earliest_full_bucket(self):
        limiter = TokenBucketLimiter(1, 0.25, self.clock, max_buckets=1)
        limiter.consume("ip:resident")
        denied = limiter.consume("ip:overflow")
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.headers["Retry-After"], "4")
        self.assertEqual(denied.headers["RateLimit-Reset"], "4")

    def test_full_least_recent_bucket_is_evicted(self):
        limiter = TokenBucketLimiter(1, 1, self.clock, max_buckets=2)
        limiter.consume("ip:first")
        limiter.consume("ip:second")
        limiter.consume("ip:first")  # Leave this one empty and recently used.
        self.now += 1
        limiter.consume("ip:first")  # Empty again; second has refilled to full.
        allowed = limiter.consume("ip:third")
        self.assertTrue(allowed.allowed)
        self.assertEqual(limiter.snapshot()["buckets"], 2)
        self.assertFalse(limiter.consume("ip:second").allowed)

    def test_override_updates_full_bucket_and_delete_resets_it(self):
        self.limiter.consume("key:key_1")
        self.limiter.set_override("key:key_1", 1, 2)
        self.assertTrue(self.limiter.consume("key:key_1").allowed)
        self.assertFalse(self.limiter.consume("key:key_1").allowed)
        self.limiter.delete_override("key:key_1")
        self.assertEqual(self.limiter.snapshot()["overrides"], {})
        self.assertTrue(self.limiter.consume("key:key_1").allowed)

    def test_admin_override_resets_a_bucket_in_a_saturated_table(self):
        limiter = TokenBucketLimiter(1, 1, self.clock, max_buckets=1)
        limiter.consume("ip:existing")
        limiter.set_override("key:key_1", 2, 1)
        self.assertEqual(limiter.snapshot()["buckets"], 1)
        self.assertTrue(limiter.consume("key:key_1").allowed)

    def test_override_limit_and_validation(self):
        limiter = TokenBucketLimiter(max_overrides=1)
        limiter.set_override("ip:a", 1, 1)
        with self.assertRaises(ApiError) as error:
            limiter.set_override("ip:b", 1, 1)
        self.assertEqual(error.exception.code, "override_limit")
        for identity in ("bad", "key:" + "x" * 65):
            with self.assertRaises(ApiError) as error:
                limiter.set_override(identity, 1, 1)
            self.assertEqual(error.exception.status, 400)
        for burst, refill in (
            (True, 1),
            (1, True),
            (100001, 1),
            (1, 0),
            (1, 10**1000),
        ):
            with self.assertRaises(ApiError):
                limiter.set_override("ip:ok", burst, refill)

    def test_production_table_and_override_caps_are_enforced(self):
        now = [100.0]
        limiter = TokenBucketLimiter(clock=lambda: now[0])
        for index in range(1000):
            limiter.consume(f"ip:{index}")
        self.assertEqual(limiter.snapshot()["buckets"], 1000)
        overflow = limiter.consume("ip:1000")
        self.assertFalse(overflow.allowed)
        self.assertEqual(limiter.snapshot()["buckets"], 1000)

        for index in range(100):
            limiter.set_override(f"client:{index}", 1, 1)
        self.assertEqual(len(limiter.snapshot()["overrides"]), 100)
        with self.assertRaises(ApiError) as error:
            limiter.set_override("client:overflow", 1, 1)
        self.assertEqual(error.exception.code, "override_limit")

    def test_parallel_burst_has_exact_capacity(self):
        limiter = TokenBucketLimiter(10, 1, self.clock)
        barrier = threading.Barrier(50)
        results = []
        result_lock = threading.Lock()

        def consume():
            barrier.wait(timeout=3)
            result = limiter.consume("client:parallel").allowed
            with result_lock:
                results.append(result)

        threads = [threading.Thread(target=consume) for _ in range(50)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(results), 10)


if __name__ == "__main__":
    unittest.main()
