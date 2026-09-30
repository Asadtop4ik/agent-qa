import re
import unittest
from unittest.mock import patch

from agent_qa.request_id import request_id


class RequestIdTests(unittest.TestCase):
    def test_valid_ids_are_preserved(self):
        for value in ("a", "abc-123", "A.B_c-9", "x" * 64):
            with self.subTest(value=value):
                self.assertEqual(request_id(value), value)

    def test_invalid_ids_generate_uuid_hex(self):
        for value in (None, "", "x" * 65, "bad id", "ümlaut", "bad/value"):
            with self.subTest(value=value):
                generated = request_id(value)
                self.assertRegex(generated, re.compile(r"^[0-9a-f]{32}$"))

    def test_invalid_id_generates_a_new_uuid(self):
        with patch("agent_qa.request_id.uuid4") as uuid4:
            uuid4.return_value.hex = "0123456789abcdef0123456789abcdef"
            self.assertEqual(request_id("bad id"), uuid4.return_value.hex)
            uuid4.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
