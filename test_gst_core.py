import unittest
from decimal import Decimal

from gst_core import calculate_invoice, indian_number


class GstCoreTests(unittest.TestCase):
    def test_intrastate_splits_cgst_sgst(self):
        result = calculate_invoice(
            [{"description": "Consulting", "quantity": 2, "unit_price": 100, "gst_rate": 18}],
            "Karnataka", "Karnataka"
        )
        self.assertEqual(result["taxable_total"], Decimal("200.00"))
        self.assertEqual(result["cgst_total"], Decimal("18.00"))
        self.assertEqual(result["sgst_total"], Decimal("18.00"))
        self.assertEqual(result["igst_total"], Decimal("0.00"))
        self.assertEqual(result["grand_total"], Decimal("236.00"))

    def test_interstate_uses_igst(self):
        result = calculate_invoice(
            [{"description": "Software", "quantity": 1, "unit_price": 1000, "gst_rate": 18}],
            "Karnataka", "Maharashtra"
        )
        self.assertEqual(result["cgst_total"], Decimal("0.00"))
        self.assertEqual(result["sgst_total"], Decimal("0.00"))
        self.assertEqual(result["igst_total"], Decimal("180.00"))
        self.assertEqual(result["grand_total"], Decimal("1180.00"))

    def test_custom_and_40_percent_rates_are_supported(self):
        result = calculate_invoice(
            [
                {"description": "Standard item", "quantity": 1, "unit_price": 100, "gst_rate": 5},
                {"description": "Notified item", "quantity": 1, "unit_price": 100, "gst_rate": 40},
                {"description": "Special item", "quantity": 1, "unit_price": 100, "gst_rate": 3},
            ],
            "Karnataka", "Maharashtra"
        )
        self.assertEqual(result["igst_total"], Decimal("48.00"))
        self.assertEqual(result["grand_total"], Decimal("348.00"))

    def test_intrastate_union_territory_splits_utgst(self):
        result = calculate_invoice(
            [{"description": "Goods", "quantity": 1, "unit_price": 100, "gst_rate": 18}],
            "Chandigarh", "Chandigarh"
        )
        self.assertEqual(result["cgst_total"], Decimal("9.00"))
        self.assertEqual(result["sgst_total"], Decimal("0.00"))
        self.assertEqual(result["utgst_total"], Decimal("9.00"))
        self.assertEqual(result["grand_total"], Decimal("118.00"))

    def test_indian_number_format(self):
        self.assertEqual(indian_number("1234567.8"), "12,34,567.80")


if __name__ == "__main__":
    unittest.main()
