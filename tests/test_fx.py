import unittest
from decimal import Decimal

from brookpay.core.errors import UnsupportedCurrency
from brookpay.fx.engine import convert, rate, to_eur


class FxEngineTests(unittest.TestCase):
    def test_same_currency_is_identity(self):
        self.assertEqual(convert("10.00", "EUR", "EUR"), Decimal("10.00"))

    def test_eur_to_usd(self):
        self.assertEqual(convert("100", "EUR", "USD"), Decimal("108.50"))

    def test_cross_rate_jpy_to_eur(self):
        self.assertEqual(convert("100000", "JPY", "EUR"), Decimal("620.35"))

    def test_to_eur_helper_matches_convert(self):
        self.assertEqual(to_eur("50", "GBP"), convert("50", "GBP", "EUR"))

    def test_rate_round_trip_close_to_one(self):
        r = rate("USD", "GBP") * rate("GBP", "USD")
        self.assertAlmostEqual(float(r), 1.0, places=9)

    def test_unknown_currency_raises(self):
        with self.assertRaises(UnsupportedCurrency):
            convert("1", "EUR", "XXX")


if __name__ == "__main__":
    unittest.main()
