"""Verify the public ledger contract independently of its implementation."""

import unittest
from decimal import Decimal

from ledger import Summary, Transaction, summarize


def transaction(transaction_id, kind, amount):
    return Transaction(transaction_id, kind, Decimal(amount))


class LedgerAcceptanceTests(unittest.TestCase):
    def test_empty_ledger(self):
        zero = Decimal("0.00")
        self.assertEqual(summarize([]), Summary(zero, zero, zero, zero, 0))

    def test_single_sale(self):
        result = summarize([transaction("sale-1", "sale", "20.00")])
        self.assertEqual(result.net, Decimal("20.00"))
        self.assertEqual(result.fee, Decimal("0.60"))
        self.assertEqual(result.posted_count, 1)

    def test_refund_reduces_net_and_fee(self):
        result = summarize([transaction("a", "sale", "70"), transaction("b", "refund", "15")])
        self.assertEqual(result.refunds, Decimal("15.00"))
        self.assertEqual(result.net, Decimal("55.00"))
        self.assertEqual(result.fee, Decimal("1.65"))

    def test_discount_applies_at_threshold(self):
        result = summarize([transaction("a", "sale", "100.00")])
        self.assertEqual(result.fee, Decimal("2.00"))

    def test_standard_rate_just_below_threshold(self):
        result = summarize([transaction("a", "sale", "99.99")])
        self.assertEqual(result.fee, Decimal("3.00"))

    def test_transaction_id_comparison_ignores_case_and_outer_whitespace(self):
        result = summarize([transaction(" Pay-01 ", "sale", "12"),
                            transaction("pay-01", "sale", "34")])
        self.assertEqual(result.posted_count, 1)
        self.assertEqual(result.sales, Decimal("12.00"))

    def test_rounds_amounts_and_fees_half_up(self):
        result = summarize([transaction("a", "sale", "1.495")])
        self.assertEqual(result.sales, Decimal("1.50"))
        self.assertEqual(result.fee, Decimal("0.05"))

    def test_rejects_negative_posted_amount(self):
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            summarize([transaction("a", "sale", "-0.01")])
