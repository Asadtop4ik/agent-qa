"""Tests for the declarative environment setting registry."""

import re
import unittest
from pathlib import Path

from agent_qa import settings


class SettingsTests(unittest.TestCase):
    def test_only_settings_or_config_read_the_process_environment(self):
        package = Path(__file__).resolve().parents[1] / "agent_qa"
        forbidden = re.compile(r"os\.environ|getenv")
        violations = []
        for path in package.rglob("*.py"):
            if path.is_symlink() or path.name in {"settings.py", "config.py"}:
                continue
            with path.open(encoding="utf-8") as source:
                for line_number, line in enumerate(source, start=1):
                    if forbidden.search(line):
                        violations.append(f"{path.name}:{line_number}")
        self.assertEqual(violations, [])

    def test_registry_parses_types_bounds_and_defaults(self):
        loaded = settings.load(
            {
                "APP_PORT": "65535",
                "AGENT_QA_RATE_REFILL_PER_SECOND": "1.25",
                "AGENT_QA_REQUIRE_IF_MATCH": "YES",
                "AGENT_QA_GIT_SHA": "build-123",
                "AGENT_QA_JOB_RETENTION": "",
            }
        )
        self.assertTrue(loaded.valid)
        self.assertEqual(loaded.values["APP_PORT"], 65535)
        self.assertEqual(loaded.values["AGENT_QA_RATE_REFILL_PER_SECOND"], 1.25)
        self.assertIs(loaded.values["AGENT_QA_REQUIRE_IF_MATCH"], True)
        self.assertEqual(loaded.values["AGENT_QA_GIT_SHA"], "build-123")
        self.assertEqual(loaded.values["AGENT_QA_JOB_RETENTION"], 100)
        self.assertEqual(loaded.sources["AGENT_QA_JOB_RETENTION"], "default")
        same_as_default = settings.load({"AGENT_QA_REQUIRE_IF_MATCH": "false"})
        description = settings.describe(same_as_default, "AGENT_QA_REQUIRE_IF_MATCH")
        self.assertEqual(description["source"], "env")
        self.assertTrue(description["is_default"])

    def test_all_numeric_settings_accept_both_edges_and_reject_outside(self):
        for name, setting in settings.SETTINGS.items():
            if setting.type not in {"int", "float"}:
                continue
            for bound in (setting.min, setting.max):
                with self.subTest(name=name, bound=bound):
                    loaded = settings.load({name: str(bound)})
                    self.assertTrue(loaded.valid)
            for value in (setting.min - 1, setting.max + 1):
                with self.subTest(name=name, value=value):
                    loaded = settings.load({name: str(value)})
                    self.assertFalse(loaded.valid)
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value):
                loaded = settings.load({"AGENT_QA_RATE_REFILL_PER_SECOND": value})
                self.assertFalse(loaded.valid)

    def test_string_bounds_empty_defaults_and_invalid_unicode(self):
        for name, minimum, maximum in (
            ("AGENT_QA_GIT_SHA", 1, 128),
            ("AGENT_QA_API_KEY", 1, 4096),
        ):
            for length in (minimum, maximum):
                with self.subTest(name=name, length=length):
                    self.assertTrue(settings.load({name: "x" * length}).valid)
            for length in (maximum + 1,):
                with self.subTest(name=name, length=length):
                    loaded = settings.load({name: "x" * length})
                    self.assertFalse(loaded.valid)
            with self.subTest(name=name, value="empty"):
                loaded = settings.load({name: ""})
                self.assertTrue(loaded.valid)
                self.assertEqual(loaded.sources[name], "default")
        loaded = settings.load({"AGENT_QA_GIT_SHA": "\ud800"})
        self.assertFalse(loaded.valid)

    def test_boolean_input_is_bounded_before_normalization(self):
        self.assertTrue(settings.load({"AGENT_QA_REQUIRE_IF_MATCH": "TRUE"}).valid)
        loaded = settings.load({"AGENT_QA_REQUIRE_IF_MATCH": "t" * 10000})
        self.assertFalse(loaded.valid)

    def test_sunset_enforcement_defaults_off_and_parses_boolean(self):
        self.assertIs(settings.load({}).values["AGENT_QA_ENFORCE_SUNSET"], False)
        self.assertIs(
            settings.load({"AGENT_QA_ENFORCE_SUNSET": "true"}).values[
                "AGENT_QA_ENFORCE_SUNSET"
            ],
            True,
        )

    def test_trace_capacity_defaults_and_is_bounded(self):
        self.assertEqual(settings.load({}).values["AGENT_QA_TRACE_CAPACITY"], 100)
        for value in ("10", "1000"):
            self.assertTrue(settings.load({"AGENT_QA_TRACE_CAPACITY": value}).valid)
        for value in ("9", "1001", "9" * 100):
            self.assertFalse(settings.load({"AGENT_QA_TRACE_CAPACITY": value}).valid)

    def test_boolean_spellings_are_case_insensitive(self):
        for value in ("true", "TRUE", "1", "yes", "Yes"):
            with self.subTest(value=value):
                loaded = settings.load({"AGENT_QA_REQUIRE_IF_MATCH": value})
                self.assertTrue(loaded.valid)
                self.assertIs(loaded.values["AGENT_QA_REQUIRE_IF_MATCH"], True)
        for value in ("false", "FALSE", "0", "no", "No"):
            with self.subTest(value=value):
                loaded = settings.load({"AGENT_QA_REQUIRE_IF_MATCH": value})
                self.assertTrue(loaded.valid)
                self.assertIs(loaded.values["AGENT_QA_REQUIRE_IF_MATCH"], False)

    def test_errors_are_safe_sorted_and_bounds_are_enforced(self):
        secret = "this-value-must-never-be-echoed"
        loaded = settings.load(
            {
                "AGENT_QA_API_KEY": secret,
                "AGENT_QA_RATE_REFILL_PER_SECOND": "nan",
                "APP_PORT": "70000",
                "AGENT_QA_REQUIRE_IF_MATCH": "perhaps",
            }
        )
        self.assertFalse(loaded.valid)
        self.assertEqual(
            [error.field for error in loaded.errors],
            sorted(error.field for error in loaded.errors),
        )
        rendered_errors = " ".join(error.message for error in loaded.errors)
        self.assertNotIn(secret, rendered_errors)
        invalid_secret = "secret-that-is-too-long"
        invalid_secret_loaded = settings.load(
            {"AGENT_QA_API_KEY": invalid_secret * 300}
        )
        self.assertNotIn(
            invalid_secret,
            " ".join(error.message for error in invalid_secret_loaded.errors),
        )
        self.assertEqual(
            next(error.message for error in loaded.errors if error.field == "APP_PORT"),
            "Must be an integer from 1 to 65535",
        )

    def test_secrets_are_redacted_without_length_or_prefix(self):
        loaded = settings.load({"AGENT_QA_API_KEY": "private-key-content"})
        description = settings.describe(loaded, "AGENT_QA_API_KEY")
        self.assertEqual(description["value"], "***")
        self.assertEqual(description["default"], "***")
        self.assertTrue(description["secret"])
        self.assertFalse(description["is_default"])

    def test_unknown_agent_qa_names_are_sorted(self):
        loaded = settings.load({"AGENT_QA_ZZZ": "x", "AGENT_QA_ABC": "y"})
        self.assertEqual(loaded.unknown_env, ("AGENT_QA_ABC", "AGENT_QA_ZZZ"))


if __name__ == "__main__":
    unittest.main()
