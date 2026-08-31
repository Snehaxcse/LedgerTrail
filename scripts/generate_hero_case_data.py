"""
Phase D hero-case fixture: ONE settlement batch, deliberately NOT solvable by
a bare "expected != received" glance. Kept structurally separate from both
the primary demo dataset (scripts/generate_synthetic_data.py) and the
held-out evaluation dataset (scripts/generate_holdout_data.py) -- this serves
neither purpose those two do. It exists purely to demonstrate the investigation
agent doing genuine multi-tool work, so it lives in its own isolated fixture
rather than adding an 8th case to the primary demo's already-verified 7/7
ground truth, or an unrelated case type to the held-out eval's precision/
recall metrics.

The scenario: three DIFFERENT orders within one batch each have a refund the
settlement file shows but the order record doesn't -- same underlying
MISSING_REFUND_RECORD pattern as the primary demo's batch 2, but bundled into
ONE exception spanning three orders with three DIFFERENT amounts (not a
uniform, suspiciously-clean number), so the aggregate unexplained_amount can
only be fully explained by individually checking all three, not by
spot-checking one and assuming the rest match. The batch is ALSO a fuzzy
(2-day) bank match -- a second, unrelated, genuinely benign fact the agent
must correctly recognize as separate from the refund issue, not fold into a
single muddled explanation.

Reuses the primary generator's fee-tier schedule, same principle as
generate_holdout_data.py -- real fee structure, not invented to make the case
easy.
"""
import datetime

from scripts.generate_synthetic_data import FEE_TIERS, GST_RATE, fee_rate_for_amount


def _rate_for_gross_paise(gross_paise):
    return fee_rate_for_amount(gross_paise / 100)


def _fee_tax(gross_paise, rate):
    fee = round(gross_paise * rate)
    tax = round(fee * GST_RATE)
    return fee, tax


def build_hero_case_dataset():
    """Returns {batch, entries, orders, bank_txn} -- one batch, six entries
    (three with the missing-refund pattern, three clean), all in paise."""
    settlement_date = datetime.date(2027, 2, 1)
    bank_date = settlement_date + datetime.timedelta(days=2)  # fuzzy match

    # (order_ref, gross_paise, refund_entry_paise) -- three different amounts,
    # deliberately not round/uniform numbers.
    affected = [
        ("HERO-01", 2_500_000, 347_250),
        ("HERO-02", 1_800_000, 219_900),
        ("HERO-03", 3_200_000, 183_600),
    ]
    clean = [
        ("HERO-04", 1_200_000),
        ("HERO-05", 2_100_000),
        ("HERO-06", 950_000),
    ]

    entries = []
    orders = []
    total_gross = total_fees = total_tax = total_refunds = total_net = 0

    for order_ref, gross, refund in affected:
        rate = _rate_for_gross_paise(gross)
        fee, tax = _fee_tax(gross, rate)
        net = gross - refund - fee - tax
        entries.append({"order_ref": order_ref, "gross_amount": gross, "fee": fee,
                          "tax": tax, "refund": refund, "net_amount": net})
        orders.append({"order_ref": order_ref, "amount": gross, "status": "completed",
                         "refund_amount": 0, "fee_amount": fee})
        total_gross += gross
        total_fees += fee
        total_tax += tax
        total_refunds += refund
        total_net += net

    for order_ref, gross in clean:
        rate = _rate_for_gross_paise(gross)
        fee, tax = _fee_tax(gross, rate)
        net = gross - fee - tax
        entries.append({"order_ref": order_ref, "gross_amount": gross, "fee": fee,
                          "tax": tax, "refund": 0, "net_amount": net})
        orders.append({"order_ref": order_ref, "amount": gross, "status": "completed",
                         "refund_amount": 0, "fee_amount": fee})
        total_gross += gross
        total_fees += fee
        total_tax += tax
        total_net += net

    batch = {
        "settlement_date": settlement_date,
        "total_gross": total_gross, "total_refunds": total_refunds,
        "total_fees": total_fees, "total_tax": total_tax, "total_net": total_net,
    }
    bank_txn = {
        "date": bank_date, "amount": total_net,
        "reference": "UTR900000000001",
        "description": "NEFT CR: HDFC BANK UTR900000000001 RAZORPAY SETTLEMENT",
    }

    expected_missing_refund_total = sum(r for _, _, r in affected)

    return {
        "batch": batch, "entries": entries, "orders": orders, "bank_txn": bank_txn,
        "affected_order_refs": [o for o, _, _ in affected],
        "expected_missing_refund_total_paise": expected_missing_refund_total,
    }
