"""Signal and in-flight request shutdown behavior without network sockets."""

import http.client
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from queue import Queue
from unittest.mock import patch

from agent_qa.server import _RequestTracker

ROOT = Path(__file__).resolve().parents[1]


class GracefulShutdownTests(unittest.TestCase):
    def test_tracker_waits_for_active_requests_but_not_idle_connections(self):
        tracker = _RequestTracker()
        self.assertTrue(tracker.wait(0.01))
        tracker.begin()
        finisher = threading.Thread(target=lambda: (time.sleep(0.03), tracker.finish()))
        finisher.start()
        started = time.monotonic()
        self.assertTrue(tracker.wait(0.5))
        self.assertGreaterEqual(time.monotonic() - started, 0.02)
        finisher.join()
        self.assertFalse(tracker.active)

    def test_sigterm_shuts_server_from_helper_thread_without_stdout(self):
        from agent_qa import server

        class FakeServer:
            def __init__(self):
                self.started = threading.Event()
                self.stopped = threading.Event()
                self.closed = False

            def serve_forever(self):
                self.started.set()
                self.stopped.wait(2)

            def shutdown(self):
                self.stopped.set()

            def server_close(self):
                self.closed = True

        fake = FakeServer()
        output = io.StringIO()
        previous = {
            sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)
        }

        def signal_server():
            self.assertTrue(fake.started.wait(1))
            os.kill(os.getpid(), signal.SIGTERM)

        def stop_workers(**kwargs):
            self.assertFalse(fake.closed)
            self.assertNotEqual(
                signal.getsignal(signal.SIGTERM), previous[signal.SIGTERM]
            )

        sender = threading.Thread(target=signal_server)
        started = time.monotonic()
        with (
            patch.object(server, "ThreadingHTTPServer", return_value=fake),
            patch.object(
                server.tenants.TENANTS,
                "shutdown",
                side_effect=stop_workers,
            ) as stop_workers,
            patch("sys.stdout", output),
        ):
            sender.start()
            server.main()
        sender.join()
        self.assertTrue(fake.closed)
        stop_workers.assert_called_once_with(timeout=5.0)
        self.assertEqual({sig: signal.getsignal(sig) for sig in previous}, previous)
        self.assertEqual(output.getvalue(), "")
        self.assertLess(time.monotonic() - started, 1)

    def test_forced_timeout_logs_to_stderr_and_stops_workers_within_deadline(self):
        from agent_qa import server

        class FakeServer:
            def __init__(self):
                self.started = threading.Event()
                self.stopped = threading.Event()
                self.closed = False

            def serve_forever(self):
                self.started.set()
                self.stopped.wait(2)

            def shutdown(self):
                self.stopped.set()

            def server_close(self):
                self.closed = True

        fake = FakeServer()
        stderr = io.StringIO()
        worker_calls = []

        def stop_workers(**kwargs):
            self.assertFalse(fake.closed)
            worker_calls.append(kwargs["timeout"])

        def send_interrupt():
            self.assertTrue(fake.started.wait(1))
            os.kill(os.getpid(), signal.SIGINT)

        loaded = type(
            "Loaded",
            (),
            {
                "errors": (),
                "values": {
                    "APP_PORT": 8080,
                    "AGENT_QA_SHUTDOWN_TIMEOUT_SECONDS": 1,
                },
            },
        )()
        sender = threading.Thread(target=send_interrupt)
        with (
            patch.object(server, "ThreadingHTTPServer", return_value=fake),
            patch.object(server.settings, "current", return_value=loaded),
            patch.object(server._REQUESTS, "wait", return_value=False),
            patch.object(server.tenants.TENANTS, "shutdown", side_effect=stop_workers),
            patch("sys.stderr", stderr),
        ):
            sender.start()
            server.main()
        sender.join()
        self.assertIn("forced", stderr.getvalue())
        self.assertTrue(fake.closed)
        self.assertLessEqual(worker_calls[0], 1)


class GracefulShutdownSubprocessTests(unittest.TestCase):
    """Loopback subprocess tests; run in CI where local sockets are available."""

    @staticmethod
    def _free_port():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    def _start(self, timeout=10):
        port = self._free_port()
        key = "graceful-subprocess-key"
        script = (
            "from agent_qa import server; "
            "server.write_access_log = lambda *args: None; server.main()"
        )
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT),
            "APP_PORT": str(port),
            "AGENT_QA_API_KEY": key,
            "AGENT_QA_SHUTDOWN_TIMEOUT_SECONDS": str(timeout),
        }
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(
                    "server exited before ready: "
                    f"{process.returncode}: {stdout} {stderr}"
                )
            try:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                connection.request("GET", "/ready")
                response = connection.getresponse()
                response.read()
                connection.close()
                if response.status == 200:
                    return process, port, key
            except OSError:
                time.sleep(0.03)
        process.terminate()
        process.communicate(timeout=2)
        raise AssertionError("server did not become ready")

    @staticmethod
    def _submit_sleep_job(port, key):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        body = json.dumps({"type": "sleep", "params": {"duration_ms": 5000}})
        connection.request(
            "POST",
            "/jobs",
            body,
            {"Content-Type": "application/json", "X-API-Key": key},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        if response.status != 202:
            raise AssertionError(f"job request failed: {response.status} {payload}")
        return payload["id"]

    @staticmethod
    def _start_long_poll(port, job_id, wait_ms):
        result = Queue()

        def request():
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
            try:
                connection.request("GET", f"/jobs/{job_id}?wait_ms={wait_ms}")
                response = connection.getresponse()
                result.put((response.status, response.read()))
            except (OSError, http.client.HTTPException) as error:
                result.put(error)
            finally:
                connection.close()

        thread = threading.Thread(target=request)
        thread.start()
        time.sleep(0.1)
        return thread, result

    def test_sigterm_drains_three_second_job_long_poll_and_exits_zero(self):
        process, port, key = self._start()
        try:
            job_id = self._submit_sleep_job(port, key)
            request, result = self._start_long_poll(port, job_id, 3000)
            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
            request.join(6)
            self.assertFalse(request.is_alive())
            self.assertEqual(result.get_nowait()[0], 200)
            self.assertEqual(process.wait(timeout=6), 0)
            stdout, stderr = process.communicate()
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "")
            self.assertLess(time.monotonic() - started, 10)
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=3)

    def test_sigint_idle_shutdown_is_quick_and_quiet(self):
        process, _, _ = self._start()
        started = time.monotonic()
        process.send_signal(signal.SIGINT)
        self.assertEqual(process.wait(timeout=2), 0)
        stdout, stderr = process.communicate()
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        self.assertLess(time.monotonic() - started, 1)

    def test_shutdown_timeout_exits_zero_and_reports_forced_to_stderr(self):
        process, port, key = self._start(timeout=1)
        try:
            job_id = self._submit_sleep_job(port, key)
            request, result = self._start_long_poll(port, job_id, 5000)
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=4), 0)
            request.join(4)
            stdout, stderr = process.communicate()
            self.assertEqual(stdout, "")
            self.assertIn("forced", stderr)
            self.assertTrue(result.get_nowait() is not None)
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
