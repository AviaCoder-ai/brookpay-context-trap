import unittest

from brookpay.utils.validation import (
    is_supported_currency,
    is_valid_user_id,
    normalize_currency,
)


class ValidationTests(unittest.TestCase):
    def test_user_id_ok(self):
        self.assertTrue(is_valid_user_id("alice"))
        self.assertTrue(is_valid_user_id("user_42-b"))

    def test_user_id_rejected(self):
        self.assertFalse(is_valid_user_id("A"))
        self.assertFalse(is_valid_user_id("9start"))
        self.assertFalse(is_valid_user_id(""))

    def test_currency_normalized(self):
        self.assertEqual(normalize_currency(" eur "), "EUR")
        self.assertTrue(is_supported_currency("thb"))
        self.assertFalse(is_supported_currency("XAU"))


if __name__ == "__main__":
    unittest.main()
