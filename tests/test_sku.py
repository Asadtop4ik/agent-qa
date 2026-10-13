import unittest

from agent_qa.sku import normalize_sku


class NormalizeSkuTests(unittest.TestCase):
    def test_normalizes_spaces_and_underscores(self):
        self.assertEqual(normalize_sku("  ab_12 cd "), "AB-12-CD")

    def test_collapses_repeated_hyphens(self):
        self.assertEqual(normalize_sku("x--y"), "X-Y")

    def test_applies_normalization_rules_together(self):
        self.assertEqual(normalize_sku("  a_b c--d  "), "A-B-C-D")

    def test_rejects_empty_result_and_non_string_values(self):
        for raw in ("", "   ", None, 123):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ValueError, "^invalid sku$"):
                    normalize_sku(raw)


if __name__ == "__main__":
    unittest.main()
