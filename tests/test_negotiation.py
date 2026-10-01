"""Unit tests for HTTP media and content-encoding negotiation."""

import unittest

from agent_qa.negotiation import (
    best_match,
    gzip_acceptable,
    parse_accept,
    parse_accept_encoding,
    prefers_problem,
)


class AcceptParsingTests(unittest.TestCase):
    def test_accept_parser_quality_defaults_parameters_and_invalid_items(self):
        cases = (
            (None, []),
            ("", []),
            (" , , ", []),
            ("application/json", [("application/json", 1.0, {})]),
            (
                "application/json; charset=utf-8",
                [("application/json", 1.0, {"charset": "utf-8"})],
            ),
            (
                "application/json;q=0.6, text/plain;q=1",
                [("application/json", 0.6, {}), ("text/plain", 1.0, {})],
            ),
            ("application/json;q=0", [("application/json", 0.0, {})]),
            ("application/json;q=1.001", []),
            ("application/json;q=-0.1", []),
            ("application/json;q=999999999999999999999999999999", []),
            ("application/json;q=NaN", []),
            ("application/json;q=inf", []),
            ("application/json;q=0.5.1", []),
            ("application/json;q=0;q=1", []),
            ("application", []),
            ("*/json", []),
            ("application/*", [("application/*", 1.0, {})]),
            ("*/*", [("*/*", 1.0, {})]),
            ("invalid, application/json", [("application/json", 1.0, {})]),
            ("application/json;q=bogus, text/plain", [("text/plain", 1.0, {})]),
        )
        for header, expected in cases:
            with self.subTest(header=header):
                self.assertEqual(parse_accept(header), expected)

    def test_best_match_uses_specificity_quality_and_offered_order(self):
        cases = (
            (
                "application/json",
                ["application/json", "text/plain"],
                "application/json",
            ),
            ("*/*", ["text/plain", "application/json"], "text/plain"),
            ("application/*", ["text/plain", "application/json"], "application/json"),
            (
                "application/*;q=0.8, application/problem+json;q=0.5",
                ["application/problem+json", "application/json"],
                "application/json",
            ),
            (
                "application/problem+json;q=0.9, application/json;q=0.8",
                ["application/json", "application/problem+json"],
                "application/problem+json",
            ),
            (
                "*/*;q=0.1, text/plain;q=0.7",
                ["application/json", "text/plain"],
                "text/plain",
            ),
            (
                "application/*;q=0, */*;q=1",
                ["application/json", "text/plain"],
                "text/plain",
            ),
            ("application/json;q=0, application/*;q=1", ["application/json"], None),
            (
                "application/problem+json;q=0, application/*;q=1",
                ["application/problem+json", "application/json"],
                "application/json",
            ),
            (
                "application/*;q=0.2, application/json;q=0.2",
                ["application/problem+json", "application/json"],
                "application/json",
            ),
            ("text/plain", ["application/json"], None),
            ("application/json;q=0", ["application/json"], None),
        )
        for header, offered, expected in cases:
            with self.subTest(header=header, offered=offered):
                self.assertEqual(best_match(header, offered), expected)

    def test_problem_json_wins_quality_ties_against_wildcard_json_ranges(self):
        cases = (
            (
                "application/problem+json;q=0.8, application/*;q=0.8",
                ["application/json", "application/problem+json"],
            ),
            (
                "application/problem+json;q=0.8, */*;q=0.8",
                ["application/json", "application/problem+json"],
            ),
        )
        for header, offered in cases:
            with self.subTest(header=header):
                self.assertEqual(
                    best_match(header, offered), "application/problem+json"
                )

    def test_best_match_accepts_parsed_ranges(self):
        parsed = parse_accept("application/*;q=0.4, application/json;q=0.8")
        self.assertEqual(
            best_match(parsed, ["application/problem+json", "application/json"]),
            "application/json",
        )

    def test_accept_header_resource_limits_are_bounded(self):
        self.assertEqual(len(parse_accept("application/json," * 101)), 100)
        self.assertEqual(parse_accept("a" * 8193), [])

    def test_problem_format_requires_positive_quality_at_least_json(self):
        cases = (
            (None, False),
            ("*/*", False),
            ("application/json", False),
            ("application/problem+json", True),
            ("application/problem+json;q=0, */*", False),
            ("application/problem+json;q=0.5, application/json;q=0.5", True),
            ("application/problem+json;q=0.5, application/json;q=0.6", False),
            ("application/problem+json;q=0.5, application/*;q=0.6", False),
            ("application/problem+json;q=0.5, */*;q=0.6", False),
            ("application/problem+json;q=0.6, application/*;q=0.5", True),
        )
        for header, expected in cases:
            with self.subTest(header=header):
                self.assertEqual(prefers_problem(header), expected)


class AcceptEncodingTests(unittest.TestCase):
    def test_encoding_parser_and_gzip_quality_rules(self):
        self.assertEqual(parse_accept_encoding(None), [])
        self.assertEqual(parse_accept_encoding(""), [])
        self.assertFalse(gzip_acceptable(None))
        self.assertFalse(gzip_acceptable("identity"))
        self.assertTrue(gzip_acceptable("gzip"))
        self.assertTrue(gzip_acceptable("gzip;q=0.5"))
        self.assertFalse(gzip_acceptable("gzip;q=0"))
        self.assertFalse(gzip_acceptable("gzip;q=0;q=1"))
        self.assertTrue(gzip_acceptable("*"))
        self.assertTrue(gzip_acceptable("*;q=0.2"))
        self.assertFalse(gzip_acceptable("*;q=1, gzip;q=0"))
        self.assertFalse(gzip_acceptable("gzip;q=0, *;q=0.5, br;q=1"))
        self.assertFalse(gzip_acceptable("gzip;q=bogus, *;q=0"))


if __name__ == "__main__":
    unittest.main()
