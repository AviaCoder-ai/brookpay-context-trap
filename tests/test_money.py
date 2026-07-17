import unittest
from decimal import Decimal

from brookpay.utils.money import quantize2, split_even, to_decimal


class MoneyTests(unittest.TestCase):
    def test_quantize_half_up(self):
        self.assertEqual(quantize2("2.005"), Decimal("2.01"))

    def test_to_decimal_from_float(self):
        self.assertEqual(to_decimal(19.99), Decimal("19.99"))

    def test_split_even_is_cent_exact(self):
        shares = split_even("100.00", 3)
        self.assertEqual(sum(shares), Decimal("100.00"))
        self.assertEqual(len(shares), 3)

    def test_to_decimal_rejects_garbage(self):
        with self.assertRaises(ValueError):
            to_decimal("not-a-number")


if __name__ == "__main__":
    unittest.main()
