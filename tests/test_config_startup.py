"""Startup validation tests that do not bind a network socket."""

import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ConfigStartupTests(unittest.TestCase):
    def test_invalid_configuration_exits_two_with_safe_stderr(self):
        env = {"PATH": os.environ.get("PATH", "")}
        invalid = {
            "APP_PORT": "70000",
            "AGENT_QA_AUDIT_CAPACITY": "0",
            "AGENT_QA_IDEMPOTENCY_TTL_SECONDS": "0",
            "AGENT_QA_JOB_RETENTION": "1001",
            "AGENT_QA_JOB_WORKERS": "4",
            "AGENT_QA_RATE_BURST": "100001",
            "AGENT_QA_RATE_REFILL_PER_SECOND": "inf",
        }
        env.update(invalid)
        result = subprocess.run(
            [sys.executable, "-c", "from agent_qa.server import main; main()"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr,
            "".join(
                f"config error: {name}: {message}\n"
                for name, message in (
                    ("AGENT_QA_AUDIT_CAPACITY", "Must be an integer from 10 to 5000"),
                    (
                        "AGENT_QA_IDEMPOTENCY_TTL_SECONDS",
                        "Must be an integer from 1 to 86400",
                    ),
                    ("AGENT_QA_JOB_RETENTION", "Must be an integer from 10 to 1000"),
                    ("AGENT_QA_JOB_WORKERS", "Must be an integer from 1 to 3"),
                    ("AGENT_QA_RATE_BURST", "Must be an integer from 1 to 100000"),
                    (
                        "AGENT_QA_RATE_REFILL_PER_SECOND",
                        "Must be a number from 0.001 to 10000.0",
                    ),
                    ("APP_PORT", "Must be an integer from 1 to 65535"),
                )
            ),
        )

    def test_import_with_invalid_configuration_has_no_error_or_output(self):
        env = {"PATH": os.environ.get("PATH", "")}
        env.update(
            {
                "APP_PORT": "not-a-port",
                "AGENT_QA_IDEMPOTENCY_TTL_SECONDS": "invalid",
                "AGENT_QA_JOB_WORKERS": "invalid",
                "AGENT_QA_JOB_RETENTION": "invalid",
                "AGENT_QA_AUDIT_CAPACITY": "invalid",
                "AGENT_QA_RATE_BURST": "invalid",
                "AGENT_QA_RATE_REFILL_PER_SECOND": "invalid",
            }
        )
        result = subprocess.run(
            [sys.executable, "-c", "import agent_qa.server"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_valid_configuration_reaches_server_loop(self):
        script = """
from unittest.mock import Mock, patch
from agent_qa import server

fake = Mock()
with patch.object(server, 'ThreadingHTTPServer', return_value=fake) as factory:
    server.main()
factory.assert_called_once_with(('0.0.0.0', 8081), server.Handler)
fake.serve_forever.assert_called_once_with()
fake.server_close.assert_called_once_with()
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env={"PATH": os.environ.get("PATH", ""), "APP_PORT": "8081"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
