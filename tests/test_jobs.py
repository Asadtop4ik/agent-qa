"""Deterministic tests for the in-memory job runner."""

import threading
import unittest
from unittest.mock import patch

from agent_qa import config
from agent_qa.errors import ApiError
from agent_qa.jobs import JobRunner, _RequestedFailure, make_builtin_handlers
from agent_qa.orders import OrderStore
from agent_qa.products import ProductStore


class JobRunnerTests(unittest.TestCase):
    def make_runner(self, *args, **kwargs):
        runner = JobRunner(*args, **kwargs)
        self.addCleanup(runner.stop, 1)
        return runner

    def test_lazy_workers_run_queued_job_and_stop(self):
        entered = threading.Event()

        def handler(_params, _progress, cancel):
            entered.set()
            cancel.wait(2)
            return {"stopped": cancel.is_set()}

        runner = self.make_runner({"sleep": handler})
        self.assertEqual(runner._threads, [])
        job = runner.submit("sleep")
        self.assertTrue(entered.wait(1))
        threads = list(runner._threads)
        runner.stop(1)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(runner.get(job["id"])["status"], "cancelled")

    def test_queued_and_running_cancellation(self):
        entered = threading.Event()
        release = threading.Event()
        observed_cancel = threading.Event()

        def handler(_params, _progress, cancel):
            entered.set()
            release.wait(2)
            if cancel.is_set():
                observed_cancel.set()
            return None

        runner = self.make_runner({"sleep": handler}, workers=1, queue_limit=2)
        self.addCleanup(release.set)
        first = runner.submit("sleep")
        self.assertTrue(entered.wait(1))
        second = runner.submit("sleep")
        cancelled = runner.cancel(second["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(runner.cancel(first["id"])["status"], "cancelling")
        self.assertEqual(runner.cancel(first["id"])["status"], "cancelling")
        release.set()
        self.assertTrue(observed_cancel.wait(1))
        self.assertEqual(runner.get(first["id"], 1000)["status"], "cancelled")
        runner.stop(1)

    def test_queue_full_and_terminal_cancel_conflict(self):
        entered = threading.Event()
        release = threading.Event()

        def handler(_params, _progress, _cancel):
            entered.set()
            release.wait(2)
            return None

        runner = self.make_runner({"sleep": handler}, workers=1, queue_limit=1)
        self.addCleanup(release.set)
        first = runner.submit("sleep")
        self.assertTrue(entered.wait(1))
        runner.submit("sleep")
        with self.assertRaises(ApiError) as error:
            runner.submit("sleep")
        self.assertEqual(error.exception.status, 503)
        release.set()
        runner.get(first["id"], wait_ms=1000)
        terminal = runner.list_jobs()["items"][0]
        with self.assertRaises(ApiError) as error:
            runner.cancel(terminal["id"])
        self.assertEqual(error.exception.status, 409)
        runner.stop(1)

    def test_condition_wait_wakes_when_job_finishes(self):
        entered = threading.Event()
        release = threading.Event()

        def handler(_params, _progress, _cancel):
            entered.set()
            release.wait(2)
            return {"done": True}

        runner = self.make_runner({"sleep": handler}, workers=1)
        self.addCleanup(release.set)
        job = runner.submit("sleep")
        self.assertTrue(entered.wait(1))
        result = []
        waiting = threading.Event()
        original_wait = runner._condition.wait

        def observe_wait(timeout=None):
            waiting.set()
            return original_wait(timeout)

        runner._condition.wait = observe_wait
        waiter = threading.Thread(
            target=lambda: result.append(runner.get(job["id"], wait_ms=5000))
        )
        waiter.start()
        self.assertTrue(waiting.wait(1))
        release.set()
        waiter.join(1)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(result[0]["status"], "succeeded")
        runner.stop(1)

    def test_handler_crash_does_not_kill_worker_and_retention_evicts_oldest(self):
        calls = 0
        entered = threading.Event()
        release = threading.Event()

        def handler(_params, _progress, _cancel):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                release.wait(2)
                raise RuntimeError("unexpected")
            return {"call": calls}

        runner = self.make_runner({"sleep": handler}, workers=1, retention=1)
        self.addCleanup(release.set)
        first = runner.submit("sleep")
        self.assertTrue(entered.wait(1))
        with self.assertLogs("agent_qa.jobs", level="ERROR") as logs:
            release.set()
            failed = runner.get(first["id"], 1000)
        self.assertEqual(failed["error"]["code"], "job_crashed")
        self.assertIn("job_id=1", logs.output[0])
        second = runner.submit("sleep")
        self.assertEqual(runner.get(second["id"], 1000)["status"], "succeeded")
        self.assertIsNone(runner.get(first["id"]))
        runner.stop(1)

    def test_builtin_jobs_and_parameter_validation(self):
        orders = OrderStore()
        products = ProductStore()
        orders.create("customer-a", 1000)
        products.create(
            sku="ITEM-1", name="Item", category="item", price_cents=100, stock=2
        )
        runner = self.make_runner(make_builtin_handlers(orders, products))
        summary = runner.submit("orders_summary")
        self.assertEqual(runner.get(summary["id"], 1000)["result"]["orders"], 1)
        report = runner.submit("stock_report", {"threshold": 2})
        self.assertEqual(
            runner.get(report["id"], 1000)["result"]["low_stock"],
            [{"product_id": 1, "sku": "ITEM-1", "stock": 2}],
        )
        with self.assertRaises(ApiError) as error:
            runner.submit("sleep", {"duration_ms": 10**100})
        self.assertEqual(error.exception.details[0]["field"], "params.duration_ms")
        failed = runner.submit("fail", {"message": "expected"})
        self.assertEqual(
            runner.get(failed["id"], 1000)["error"],
            {"code": "job_failed", "message": "expected"},
        )
        runner.stop(1)

    def test_start_is_explicit_and_workers_are_daemon(self):
        runner = self.make_runner({"sleep": lambda *_args: None}, workers=2)
        self.assertEqual(runner._threads, [])
        runner.start()
        self.assertEqual(len(runner._threads), 2)
        self.assertTrue(all(thread.daemon for thread in runner._threads))
        runner.stop(1)

    def test_timed_out_stop_recovers_after_all_workers_finish(self):
        for recovery in ("submit", "start"):
            with self.subTest(recovery=recovery):
                started = [threading.Event(), threading.Event()]
                release = [threading.Event(), threading.Event()]
                worker_threads = {}

                def handler(params, _progress, _cancel):
                    worker = params["duration_ms"]
                    if worker == 2:
                        return {"recovered": True}
                    worker_threads[worker] = threading.current_thread()
                    started[worker].set()
                    release[worker].wait(2)
                    return None

                runner = self.make_runner({"sleep": handler}, workers=2)
                for event in release:
                    self.addCleanup(event.set)
                for worker in range(2):
                    runner.submit("sleep", {"duration_ms": worker})
                for event in started:
                    self.assertTrue(event.wait(1))

                runner.stop(0)
                with self.assertRaises(ApiError) as error:
                    runner.submit("sleep", {"duration_ms": 2})
                self.assertEqual(error.exception.status, 503)

                release[0].set()
                worker_threads[0].join(1)
                self.assertFalse(worker_threads[0].is_alive())
                self.assertTrue(worker_threads[1].is_alive())
                with self.assertRaises(ApiError) as error:
                    runner.submit("sleep", {"duration_ms": 2})
                self.assertEqual(error.exception.status, 503)

                release[1].set()
                worker_threads[1].join(1)
                self.assertFalse(worker_threads[1].is_alive())

                if recovery == "start":
                    runner.start()
                recovered = runner.submit("sleep", {"duration_ms": 2})
                self.assertEqual(
                    runner.get(recovered["id"], 1000)["result"],
                    {"recovered": True},
                )

    def test_progress_result_isolated_and_sleep_zero_succeeds(self):
        started = threading.Event()
        release = threading.Event()

        def handler(_params, progress, _cancel):
            progress(45)
            started.set()
            release.wait(2)
            return {"nested": {"values": [1]}}

        runner = self.make_runner({"sleep": handler})
        self.addCleanup(release.set)
        job = runner.submit("sleep")
        self.assertTrue(started.wait(1))
        running = runner.get(job["id"])
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["progress"], 45)
        release.set()
        finished = runner.get(job["id"], 1000)
        finished["result"]["nested"]["values"].append(2)
        self.assertEqual(runner.get(job["id"])["result"], {"nested": {"values": [1]}})

        builtins = self.make_runner(make_builtin_handlers(OrderStore(), ProductStore()))
        zero = builtins.submit("sleep", {"duration_ms": 0})
        self.assertEqual(builtins.get(zero["id"], 1000)["result"], {"slept_ms": 0})

    def test_cancellation_wins_when_handler_raises(self):
        for failure in (RuntimeError("unexpected"), _RequestedFailure("expected")):
            with self.subTest(failure=type(failure).__name__):
                entered = threading.Event()
                release = threading.Event()

                def handler(_params, _progress, _cancel):
                    entered.set()
                    release.wait(2)
                    raise failure

                runner = self.make_runner({"sleep": handler})
                self.addCleanup(release.set)
                job = runner.submit("sleep")
                self.assertTrue(entered.wait(1))
                self.assertEqual(runner.cancel(job["id"])["status"], "cancelling")
                if isinstance(failure, RuntimeError):
                    with self.assertLogs("agent_qa.jobs", level="ERROR"):
                        release.set()
                        cancelled = runner.get(job["id"], 1000)
                else:
                    release.set()
                    cancelled = runner.get(job["id"], 1000)
                self.assertEqual(cancelled["status"], "cancelled")
                self.assertIsNone(cancelled["result"])
                self.assertIsNone(cancelled["error"])

    def test_stop_cancels_active_and_queued_jobs(self):
        entered = threading.Event()

        def handler(_params, _progress, cancel):
            entered.set()
            cancel.wait(2)
            return None

        runner = self.make_runner(
            {"sleep": handler}, workers=1, queue_limit=2, retention=1
        )
        active = runner.submit("sleep")
        self.assertTrue(entered.wait(1))
        queued = runner.submit("sleep")
        runner.stop(1)
        self.assertEqual(runner.get(active["id"])["status"], "cancelled")
        self.assertIsNone(runner.get(queued["id"]))

    def test_job_settings_defaults_and_bounds(self):
        with patch.dict(
            "os.environ",
            {"AGENT_QA_JOB_WORKERS": "2", "AGENT_QA_JOB_RETENTION": "100"},
        ):
            self.assertEqual(config.job_workers(), 2)
            self.assertEqual(config.job_retention(), 100)
        for name, function, valid, invalid in (
            (
                "AGENT_QA_JOB_WORKERS",
                config.job_workers,
                ("1", "3"),
                ("0", "4", "1" * 100),
            ),
            (
                "AGENT_QA_JOB_RETENTION",
                config.job_retention,
                ("10", "1000"),
                ("9", "1001", "1" * 100),
            ),
        ):
            for value in valid:
                with patch.dict("os.environ", {name: value}):
                    self.assertGreaterEqual(function(), 1)
            for value in invalid:
                with patch.dict("os.environ", {name: value}):
                    expected_default = 2 if name.endswith("WORKERS") else 100
                    self.assertEqual(function(), expected_default)

    def test_builtin_summary_counts_revenue_stock_order_and_defaults(self):
        orders = OrderStore()
        for index, total in enumerate((100, 200, 300, 400)):
            order = orders.create(f"customer-{index}", total)
            if index == 0:
                orders.update(order["id"], {"status": "paid"})
                orders.update(order["id"], {"status": "shipped"})
            elif index == 1:
                orders.update(order["id"], {"status": "paid"})
            elif index == 2:
                orders.update(order["id"], {"status": "cancelled"})
        products = ProductStore()
        for sku, stock in (
            ("ITEM-1", 2),
            ("ITEM-2", 1),
            ("ITEM-3", 2),
            ("ITEM-4", 6),
        ):
            products.create(
                sku=sku,
                name=sku,
                category="item",
                price_cents=10,
                stock=stock,
            )
        runner = self.make_runner(make_builtin_handlers(orders, products))
        summary = runner.submit("orders_summary")
        summary_result = runner.get(summary["id"], 1000)["result"]
        self.assertEqual(
            summary_result,
            {
                "orders": 4,
                "by_status": {
                    "new": 1,
                    "paid": 1,
                    "shipped": 1,
                    "cancelled": 1,
                },
                "revenue_cents": 300,
            },
        )
        report = runner.submit("stock_report")
        self.assertEqual(
            runner.get(report["id"], 1000)["result"],
            {
                "threshold": 5,
                "low_stock": [
                    {"product_id": 2, "sku": "ITEM-2", "stock": 1},
                    {"product_id": 1, "sku": "ITEM-1", "stock": 2},
                    {"product_id": 3, "sku": "ITEM-3", "stock": 2},
                ],
            },
        )
        failed = runner.submit("fail")
        self.assertEqual(
            runner.get(failed["id"], 1000)["error"],
            {"code": "job_failed", "message": "boom"},
        )

    def test_parameter_types_unknown_fields_and_wait_bounds(self):
        runner = self.make_runner({"sleep": lambda *_args: None})
        invalid = (
            ("sleep", {"duration_ms": True}, "params.duration_ms"),
            ("sleep", {"duration_ms": "2"}, "params.duration_ms"),
            ("sleep", {"duration_ms": 10**100}, "params.duration_ms"),
            ("sleep", {"other": 1}, "params.other"),
            ("stock_report", {"threshold": False}, "params.threshold"),
            ("stock_report", {"threshold": "5"}, "params.threshold"),
            ("fail", {"message": 5}, "params.message"),
            ("orders_summary", {"unknown": True}, "params.unknown"),
        )
        for job_type, params, field in invalid:
            with self.subTest(job_type=job_type, params=params):
                with self.assertRaises(ApiError) as error:
                    runner.submit(job_type, params)
                self.assertEqual(error.exception.details[0]["field"], field)
        for value in (True, -1, 5001, 10**100):
            with self.subTest(wait_ms=value):
                with self.assertRaises(ApiError):
                    runner.get(1, wait_ms=value)
        self.assertEqual(runner.submit("sleep")["params"], {"duration_ms": 100})

    def test_stop_rejects_nonfinite_and_huge_timeouts(self):
        runner = self.make_runner({"sleep": lambda *_args: None})
        for timeout in (-1, float("inf"), float("nan"), 10**10000):
            with self.subTest(timeout_type=type(timeout).__name__):
                with self.assertRaises(ValueError):
                    runner.stop(timeout)


if __name__ == "__main__":
    unittest.main()
