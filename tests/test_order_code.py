import unittest

from agent_qa.order_code import format_order_code


class FormatOrderCodeTests(unittest.TestCase):
    def test_formats_positive_order_ids(self):
        self.assertEqual(format_order_code(42), "ORD-000042")
        self.assertEqual(format_order_code(1), "ORD-000001")
        self.assertEqual(format_order_code(1234567), "ORD-1234567")

    def test_rejects_non_positive_and_non_integer_order_ids(self):
        for order_id in (0, -1, True, False, "1", 1.0, None):
            with self.subTest(order_id=order_id):
                with self.assertRaisesRegex(
                    ValueError, "^order_id must be a positive integer$"
                ):
                    format_order_code(order_id)


if __name__ == "__main__":
    unittest.main()
