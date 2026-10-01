"""Unit tests for bounded Accept and Accept-Encoding parsing."""

import unittest

from agent_qa.negotiation import (
    MIN_GZIP_BYTES,
    best_match,
    gzip_acceptable,
    parse_accept,
    parse_accept_encoding,
    prefers_problem,
)


class NegotiationTests(unittest.TestCase):
    def test_accept_parser_quality_wildcards_and_malformed_members(self):
        cases = (
            (None, []),
            ("", []),
            ("application/json", [("application/json", 1.0, {})]),
            ("application/json;q=0.4", [("application/json", 0.4, {})]),
            ("\tapplication/json;\tq=0.4\t", [("application/json", 0.4, {})]),
            ("*/*", [("*/*", 1.0, {})]),
            ("application/*;q=0", [("application/*", 0.0, {})]),
            (
                "application/json;profile=compact",
                [("application/json", 1.0, {"profile": "compact"})],
            ),
            (
                'application/json;profile="compact"',
                [("application/json", 1.0, {"profile": "compact"})],
            ),
            (
                "application/json;;q=0.5",
                [("application/json", 0.5, {})],
            ),
            (
                "text/plain;q=0.8, application/json;q=0.3",
                [("text/plain", 0.8, {}), ("application/json", 0.3, {})],
            ),
            ("application/json;q=bogus", []),
            ("application/json;q=1.001", []),
            ("application/json;q=0.١", []),
            ("application/json;q=-1", []),
            ("*/json", []),
            ("application", []),
            ("application/json;bad", []),
            ("application/json;q=0.5;q=0.7", []),
            ('application/json;profile="unterminated', []),
            ('application/json;profile="bad\x01value"', []),
            ("application/json\r\n, text/plain", []),
            ("application/json, nonsense", [("application/json", 1.0, {})]),
            ("x" * 8193, []),
            (
                ",".join(["application/json"] * 100 + ["application/problem+json;q=0"]),
                [],
            ),
            ("application/json;" + ";".join(["x=y"] * 101), []),
        )
        self.assertEqual(MIN_GZIP_BYTES, 256)
        for header, expected in cases:
            with self.subTest(header=header):
                self.assertEqual(parse_accept(header), expected)

    def test_best_match_uses_specificity_for_effective_quality(self):
        accept = parse_accept("application/json;q=0.1, application/*;q=0.8, */*;q=1")
        self.assertEqual(
            best_match(accept, ("application/json", "application/problem+json")),
            "application/problem+json",
        )
        self.assertEqual(
            best_match(
                parse_accept("application/json;q=0, */*;q=1"),
                ("application/json",),
            ),
            None,
        )
        self.assertEqual(
            best_match(
                parse_accept("text/*;q=0.8, */*;q=0.1"),
                ("text/plain", "application/json"),
            ),
            "text/plain",
        )

    def test_problem_preference_compares_effective_json_quality(self):
        cases = (
            (None, False),
            ("*/*", False),
            ("*/*;q=0.5", False),
            ("application/problem+json", True),
            ("application/problem+json;q=0.6, application/json;q=0.6", True),
            ("application/problem+json;q=0.5, application/json;q=0.6", False),
            ("application/problem+json;q=0, */*;q=1", False),
            ("application/problem+json;q=0.6, application/*;q=0.9", False),
            (
                "application/problem+json;q=0.6, application/json;q=0, "
                "application/*;q=0.9",
                True,
            ),
            ("application/*", True),
            ("text/plain", False),
        )
        for header, expected in cases:
            with self.subTest(header=header):
                self.assertEqual(prefers_problem(header), expected)

    def test_accept_encoding_parser_and_gzip_quality(self):
        self.assertEqual(
            parse_accept_encoding("gzip, br;q=0.5, *;q=0"),
            [("gzip", 1.0), ("br", 0.5), ("*", 0.0)],
        )
        cases = (
            (None, False),
            ("", False),
            ("gzip", True),
            ("GZIP;q=0.2", True),
            ("gzip;q=0", False),
            ("gzip;q=0, gzip;q=1", False),
            ("gzip;q=0, *;q=1", False),
            ("*", True),
            ("br, *;q=0.3", True),
            ("gzip;q=bogus", False),
        )
        for header, expected in cases:
            with self.subTest(header=header):
                self.assertEqual(gzip_acceptable(header), expected)


if __name__ == "__main__":
    unittest.main()
