import json
import subprocess
import sys
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

    def test_item_etags_do_not_repeat_after_a_process_restart(self):
        old_etag = etag_for("product", 1, 1)
        child_code = """
import json
import sys
from agent_qa.conditional import (
    PreconditionFailed,
    check_expected_version,
    etag_for,
    parse_etag_list,
    weak_match,
)

old_etag = sys.argv[1]
current_etag = etag_for("product", 1, 1)
try:
    check_expected_version(parse_etag_list(old_etag), "product", 1, 1)
except PreconditionFailed:
    old_write_rejected = True
else:
    old_write_rejected = False
print(json.dumps({
    "etag": current_etag,
    "old_read_matched": weak_match(parse_etag_list(old_etag), current_etag),
    "old_write_rejected": old_write_rejected,
}))
"""
        result = subprocess.run(
            [sys.executable, "-c", child_code, old_etag],
            check=True,
            capture_output=True,
            text=True,
        )
        after_restart = json.loads(result.stdout)

        self.assertNotEqual(after_restart["etag"], old_etag)
        self.assertFalse(after_restart["old_read_matched"])
        self.assertTrue(after_restart["old_write_rejected"])

    def test_matching_and_generated_tags(self):
        current = etag_for("order", 7, 3)
        self.assertEqual(current, etag_for("order", 7, 3))
        changed = etag_for("order", 7, 4)
        self.assertNotEqual(current, changed)
        self.assertTrue(strong_match((f"W/{current}", current), current))
        self.assertFalse(strong_match((f"W/{current}",), current))
        self.assertTrue(weak_match((f"W/{current}",), current))
        weak_current = f"W/{current}"
        self.assertTrue(weak_match((current,), weak_current))
        self.assertTrue(weak_match((weak_current,), weak_current))
        self.assertFalse(strong_match((current,), weak_current))
        self.assertFalse(strong_match((weak_current,), weak_current))
        self.assertTrue(weak_match("*", current))


if __name__ == "__main__":
    unittest.main()
