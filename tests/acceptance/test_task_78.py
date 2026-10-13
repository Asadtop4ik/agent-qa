import unittest

from agent_qa.sku import normalize_sku


class NormalizeSkuAcceptanceTests(unittest.TestCase):
    def test_ac1_normalizes_example_sku(self):
        self.assertEqual(normalize_sku("  ab_12 cd "), "AB-12-CD")

    def test_ac2_collapses_repeated_hyphens(self):
        self.assertEqual(normalize_sku("x--y"), "X-Y")

    def test_ac3_applies_all_normalization_rules(self):
        self.assertEqual(normalize_sku("  a_b c--d  "), "A-B-C-D")

    def test_ac4_rejects_empty_result_and_non_string_arguments(self):
        invalid_skus = ("   ", None, 123)
        for raw in invalid_skus:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as error:
                    normalize_sku(raw)
                self.assertEqual(str(error.exception), "invalid sku")


if __name__ == "__main__":
    unittest.main()
