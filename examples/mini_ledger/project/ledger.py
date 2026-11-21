"""Summarize posted sales and refunds for one reporting period."""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


@dataclass(frozen=True)
class Transaction:
    transaction_id: str
    kind: str
    amount: Decimal


@dataclass(frozen=True)
class Summary:
    sales: Decimal
    refunds: Decimal
    net: Decimal
    fee: Decimal
    posted_count: int


_CENT = Decimal("0.01")
_ZERO = Decimal("0.00")
_DISCOUNT_THRESHOLD = Decimal("100.00")
_STANDARD_RATE = Decimal("0.03")
_DISCOUNT_RATE = Decimal("0.02")


def _money(amount: Decimal) -> Decimal:
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


def summarize(transactions: Iterable[Transaction]) -> Summary:
    """Return two-decimal totals and the count of distinct posted transactions."""
    sales = refunds = _ZERO
    posted_ids = set()
    for transaction in transactions:
        transaction_id = transaction.transaction_id.strip()
        if not transaction_id:
            raise ValueError("transaction ID cannot be empty")
        if transaction_id in posted_ids:
            continue

        amount = transaction.amount
        if not amount.is_finite() or amount < _ZERO:
            raise ValueError("transaction amount must be finite and nonnegative")
        if transaction.kind == "sale":
            sales += _money(amount)
        elif transaction.kind == "refund":
            refunds += _money(amount)
        else:
            raise ValueError(f"unsupported transaction kind: {transaction.kind}")
        posted_ids.add(transaction_id)

    net = sales + refunds
    rate = _DISCOUNT_RATE if net > _DISCOUNT_THRESHOLD else _STANDARD_RATE
    fee = _money(max(net, _ZERO) * rate)
    return Summary(sales, refunds, net, fee, len(posted_ids))
