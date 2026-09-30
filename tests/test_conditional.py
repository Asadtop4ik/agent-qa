import unittest

from agent_qa.conditional import (
    etag_for,
    parse_etag_list,
    strong_match,
    weak_match,
)
from agent_qa.errors import ApiError


class ConditionalParserTests(unittest.TestCase):
    def test_parse_wildcard_and_tag_lists(self):
        self.assertEqual(parse_etag_list(" * "), "*")
        self.assertEqual(parse_etag_list(' "o1.2", W/"o1.3" '), ('"o1.2"', 'W/"o1.3"'))
        self.assertEqual(parse_etag_list('"opaque,tag"'), ('"opaque,tag"',))

    def test_invalid_values_are_bad_preconditions(self):
        too_many_tags = ",".join('"x"' for _ in range(101))
        for raw in (
            "",
            "   ",
            '"unterminated',
            "unquoted",
            '"x",, "y"',
            '*, "x"',
            '"x\x01y"',
            '"a""b"',
            "x" * 8193,
            too_many_tags,
        ):
            with self.subTest(raw=raw), self.assertRaises(ApiError) as raised:
                parse_etag_list(raw)
            self.assertEqual(raised.exception.status, 400)
            self.assertEqual(raised.exception.code, "invalid_precondition")

    def test_matching_and_generated_tags(self):
        current = etag_for("order", 7, 3)
        self.assertEqual(current, '"o7.3"')
        self.assertEqual(etag_for("product", 2, 1), '"p2.1"')
        self.assertTrue(strong_match(('W/"o7.3"', current), current))
        self.assertFalse(strong_match(('W/"o7.3"',), current))
        self.assertTrue(weak_match(('W/"o7.3"',), current))
        weak_current = 'W/"o7.3"'
        self.assertTrue(weak_match((current,), weak_current))
        self.assertTrue(weak_match((weak_current,), weak_current))
        self.assertFalse(strong_match((current,), weak_current))
        self.assertFalse(strong_match((weak_current,), weak_current))
        self.assertTrue(weak_match("*", current))


if __name__ == "__main__":
    unittest.main()
