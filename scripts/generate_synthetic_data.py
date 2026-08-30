"""
Generates synthetic Razorpay-style settlement reconciliation data for LedgerTrail (Day 1).

Produces four raw CSVs (data/settlement_batches.csv, data/razorpay_settlement.csv,
data/bank_statement.csv, data/order_records.csv) and data/ground_truth.json
documenting every error deliberately injected into the dataset. This script has
no database side-effect of its own -- scripts/ingest.py is the sole path that
populates the database, reading these exact CSVs (see float-to-paise migration
Phase 1 follow-up: this script used to also write rupee floats directly to the
DB via its own write_to_db(), a second code path that duplicated what ingest.py
already did and was always immediately wiped and overwritten by it in the real
pipeline -- app/startup.py's generate-then-ingest sequence never read the DB in
between the two calls. Removed rather than converted to paise, since keeping
two independent DB-writing paths in sync was the actual risk, not which unit
either of them used.)

No matching, bridge, or exception logic lives here -- this script only produces
raw source data and the answer key for it. All math here is plain deterministic
Python (fee tiers, GST, rounding), never an LLM.
"""
import csv
import datetime
import json
import random
from pathlib import Path

random.seed(42)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# Plausible Razorpay-style tiered fee schedule (rate applies below the threshold).
FEE_TIERS = [
    (1000, 0.020),
    (5000, 0.023),
    (float("inf"), 0.025),
]
GST_RATE = 0.18  # GST charged on the payment gateway fee, standard Indian practice.
NATURAL_REFUND_PROBABILITY = 0.12  # organic refunds unrelated to the injected error
FEE_DRIFT_MULTIPLIER = 1.20  # ~20% systemic fee drift, applied uniformly across a batch

BATCH_CONFIGS = [
    {"batch_num": 1, "settlement_date": datetime.date(2026, 8, 20), "num_entries": 17, "timing_mismatch": False},
    {"batch_num": 2, "settlement_date": datetime.date(2026, 8, 23), "num_entries": 19, "timing_mismatch": False},
    {"batch_num": 3, "settlement_date": datetime.date(2026, 8, 26), "num_entries": 16, "timing_mismatch": False},
    {"batch_num": 4, "settlement_date": datetime.date(2026, 8, 27), "num_entries": 15, "timing_mismatch": True},
    # Batches 5-10: appended after the original 4 so their random draws happen
    # strictly later in the fixed-seed sequence -- batches 1-4's own data is
    # untouched. All clean (no INJECTED_ROLES entry, timing_mismatch=False),
    # spread across ~2.5 months so the reconciled dataset spans a longer range.
    {"batch_num": 5, "settlement_date": datetime.date(2026, 9, 3), "num_entries": 18, "timing_mismatch": False},
    {"batch_num": 6, "settlement_date": datetime.date(2026, 9, 10), "num_entries": 16, "timing_mismatch": False},
    {"batch_num": 7, "settlement_date": datetime.date(2026, 9, 24), "num_entries": 19, "timing_mismatch": False},
    {"batch_num": 8, "settlement_date": datetime.date(2026, 10, 8), "num_entries": 17, "timing_mismatch": False},
    # fee_drift is applied as a deterministic post-processing step on batch 9's
    # already-generated rows (see apply_fee_drift) -- it consumes NO random draws,
    # so it cannot shift any other batch's data regardless of position.
    {"batch_num": 9, "settlement_date": datetime.date(2026, 10, 22), "num_entries": 15, "timing_mismatch": False, "fee_drift": True},
    {"batch_num": 10, "settlement_date": datetime.date(2026, 11, 5), "num_entries": 18, "timing_mismatch": False},
]

# Exactly one occurrence of each injected error type, placed by (batch_num, index in batch).
# Each batch carries exactly one error type per role (batch 1, and batches 7-10,
# are left completely clean on purpose) so a single-classification-per-batch
# reconciliation engine can still be exercised against every error type in isolation.
#
# (5, 0) and (6, 0) were added after batches 1-10 already existed. Index 0 was
# deliberately chosen in each case to keep the fixed-seed sequence byte-identical
# for every other batch:
#   - (5, 0): the missing_refund branch and the natural no-refund branch both
#     consume exactly one random() call (random.uniform() and random.random()
#     are each a single call under the hood), so swapping an order's role from
#     "natural, no refund" to "missing_refund" costs zero net draws -- PROVIDED
#     that order had no natural refund to begin with. OD-05-0001 happened to
#     have none. This was confirmed the only way that actually matters: running
#     the full regeneration with this change applied and diffing every other
#     batch's output against a pre-change snapshot, byte for byte -- not by
#     reasoning about draw counts in isolation (an earlier, simplified dry run
#     used to pick this index skipped the make_bank_reference() call each batch
#     also makes, which shifts the real stream position; it arrived at the
#     right index anyway, but only the full-file diff is the actual proof).
#     If this index is ever changed, re-verify the same way: full regen, diff
#     every batch that should be untouched.
#   - (6, 0): duplicate_entry is applied entirely in build_dataset() after
#     build_batch_orders() has already returned; it never touches random(), so
#     any index is safe here regardless of natural refund outcome.
INJECTED_ROLES = {
    (2, 0): "missing_refund",
    (2, 1): "wrong_fee_tier",
    (3, 0): "duplicate_entry",
    (5, 0): "missing_refund",
    (6, 0): "duplicate_entry",
}


def round2(x):
    return round(x, 2)


def apply_fee_drift(batch_settlement_rows, batch_order_rows):
    """Increases every entry's fee by FEE_DRIFT_MULTIPLIER (~20%) and mirrors the
    SAME drifted value into the matching OrderRecord.fee_amount, so nothing looks
    wrong from the order system's own perspective -- no per-order FEE_TIER_MISMATCH
    fires, since entry.fee and order.fee_amount agree with each other. The drift is
    only visible as a batch-wide statistical outlier (see app/anomaly_detection.py).
    Mutates both lists of dicts in place. Pure arithmetic -- consumes no random()
    calls, so it cannot affect any other batch's data."""
    order_by_ref = {o["order_ref"]: o for o in batch_order_rows}

    for row in batch_settlement_rows:
        drifted_fee = round2(row["fee"] * FEE_DRIFT_MULTIPLIER)
        drifted_tax = round2(drifted_fee * GST_RATE)
        row["fee"] = drifted_fee
        row["tax"] = drifted_tax
        row["net_amount"] = round2(row["gross_amount"] - drifted_fee - drifted_tax - row["refund"])

        order = order_by_ref.get(row["order_ref"])
        if order is not None:
            order["fee_amount"] = drifted_fee


def fee_rate_for_amount(amount):
    for threshold, rate in FEE_TIERS:
        if amount < threshold:
            return rate
    return FEE_TIERS[-1][1]


def wrong_fee_rate(correct_rate):
    """Pick a different, still-plausible tier rate than the correct one."""
    rates = [r for _, r in FEE_TIERS]
    idx = rates.index(correct_rate)
    return rates[idx - 1] if idx > 0 else rates[idx + 1]


def compute_fee_tax(gross_amount, rate):
    fee = round2(gross_amount * rate)
    tax = round2(fee * GST_RATE)
    return fee, tax


def make_order_ref(batch_num, seq):
    return f"OD-{batch_num:02d}-{seq:04d}"


def make_bank_reference():
    return f"UTR{random.randint(100000000000, 999999999999)}"


def build_batch_orders(batch_num, num_entries):
    """Build the 'true' economics for every order in a batch before any error injection."""
    orders = []
    for i in range(num_entries):
        gross_amount = round2(random.uniform(500, 15000))
        rate = fee_rate_for_amount(gross_amount)
        fee, tax = compute_fee_tax(gross_amount, rate)

        role = INJECTED_ROLES.get((batch_num, i))

        if role == "missing_refund":
            refund = round2(gross_amount * random.uniform(0.3, 0.8))
        elif random.random() < NATURAL_REFUND_PROBABILITY:
            refund = round2(gross_amount * random.uniform(0.2, 1.0))
        else:
            refund = 0.0

        status = "completed"
        if refund >= gross_amount * 0.99:
            status = "refunded"
        elif refund > 0:
            status = "partially_refunded"

        if role == "missing_refund":
            # The merchant's own system has no idea a refund happened -- that's
            # the whole point of this error. OrderRecord must look self-consistent
            # (completed, refund_amount=0); only the settlement file shows the truth.
            status = "completed"

        orders.append(
            {
                "order_ref": make_order_ref(batch_num, i + 1),
                "gross_amount": gross_amount,
                "fee": fee,
                "tax": tax,
                "refund": refund,
                "status": status,
                "role": role,
            }
        )
    return orders


def build_dataset():
    """Returns (batches, settlement_rows, order_rows, bank_rows, ground_truth)."""
    batches = []
    settlement_rows = []  # dicts: batch_num, order_ref, gross_amount, fee, tax, refund, net_amount
    order_rows = []  # dicts: order_ref, amount, status, refund_amount, fee_amount
    bank_rows = []  # dicts: batch_num, date, amount, reference, description
    ground_truth = []

    for cfg in BATCH_CONFIGS:
        batch_num = cfg["batch_num"]
        orders = build_batch_orders(batch_num, cfg["num_entries"])
        batch_settlement_start = len(settlement_rows)
        batch_order_start = len(order_rows)

        batch_total_gross = 0.0
        batch_total_fees = 0.0
        batch_total_tax = 0.0
        batch_total_refunds = 0.0
        batch_total_net = 0.0

        for order in orders:
            role = order["role"]

            expected_fee = order["fee"]
            expected_tax = order["tax"]

            settlement_fee = expected_fee
            settlement_tax = expected_tax
            order_refund_amount = order["refund"]

            if role == "wrong_fee_tier":
                correct_rate = fee_rate_for_amount(order["gross_amount"])
                bad_rate = wrong_fee_rate(correct_rate)
                settlement_fee, settlement_tax = compute_fee_tax(order["gross_amount"], bad_rate)
                ground_truth.append(
                    {
                        "type": "wrong_fee_tier",
                        "order_ref": order["order_ref"],
                        "batch_id": batch_num,
                        "expected_value": expected_fee,
                        "actual_value": settlement_fee,
                        "description": (
                            f"{order['order_ref']}: settlement fee is Rs.{settlement_fee} "
                            f"(rate {bad_rate:.1%}) but the correct tier for a "
                            f"Rs.{order['gross_amount']} order is {correct_rate:.1%}, "
                            f"i.e. Rs.{expected_fee} -- order record still shows the correct expected fee."
                        ),
                    }
                )

            if role == "missing_refund":
                order_refund_amount = 0.0
                ground_truth.append(
                    {
                        "type": "missing_refund",
                        "order_ref": order["order_ref"],
                        "batch_id": batch_num,
                        "expected_value": order["refund"],
                        "actual_value": 0.0,
                        "description": (
                            f"{order['order_ref']}: settlement deducted a Rs.{order['refund']} refund, "
                            f"but the order record shows no refund at all (refund_amount=0)."
                        ),
                    }
                )

            settlement_net = round2(
                order["gross_amount"] - settlement_fee - settlement_tax - order["refund"]
            )

            settlement_rows.append(
                {
                    "batch_num": batch_num,
                    "order_ref": order["order_ref"],
                    "gross_amount": order["gross_amount"],
                    "fee": settlement_fee,
                    "tax": settlement_tax,
                    "refund": order["refund"],
                    "net_amount": settlement_net,
                }
            )

            if role == "duplicate_entry":
                settlement_rows.append(dict(settlement_rows[-1]))
                ground_truth.append(
                    {
                        "type": "duplicate_entry",
                        "order_ref": order["order_ref"],
                        "batch_id": batch_num,
                        "expected_value": "1 settlement entry",
                        "actual_value": "2 settlement entries (identical)",
                        "description": (
                            f"{order['order_ref']}: appears twice in the settlement batch with "
                            f"identical amounts, but the bank credit only reflects it once."
                        ),
                    }
                )

            order_rows.append(
                {
                    "order_ref": order["order_ref"],
                    "amount": order["gross_amount"],
                    "status": order["status"],
                    "refund_amount": order_refund_amount,
                    "fee_amount": expected_fee,
                }
            )

            # Batch totals reflect what actually happened to the settlement money
            # (the duplicate row above is a data artifact, not a second real payout).
            batch_total_gross += order["gross_amount"]
            batch_total_fees += settlement_fee
            batch_total_tax += settlement_tax
            batch_total_refunds += order["refund"]
            batch_total_net += settlement_net

        if cfg.get("fee_drift"):
            batch_settlement_rows = settlement_rows[batch_settlement_start:]
            batch_order_rows = order_rows[batch_order_start:]
            # Captured before mutation: what this batch's own fee_rate would have
            # been without the drift, i.e. the "expected" side of the ground-truth
            # entry below.
            pre_drift_fee_rate = batch_total_fees / batch_total_gross
            apply_fee_drift(batch_settlement_rows, batch_order_rows)
            # Totals must reflect the drifted fees, not the pre-drift accumulation
            # above -- re-derive them from the (now mutated) rows rather than patch
            # the running sums, since that's less error-prone than tracking deltas.
            batch_total_fees = sum(r["fee"] for r in batch_settlement_rows)
            batch_total_tax = sum(r["tax"] for r in batch_settlement_rows)
            batch_total_net = sum(r["net_amount"] for r in batch_settlement_rows)
            # gross and refunds are untouched by fee drift -- no change needed.
            post_drift_fee_rate = batch_total_fees / batch_total_gross

            # Unlike every other injected role above, this error is batch-wide
            # (no single order_ref) -- it's only visible as a statistical outlier
            # in fee_rate (see app/anomaly_detection.py), not any per-order mismatch.
            ground_truth.append(
                {
                    "type": "systemic_fee_drift",
                    "order_ref": None,
                    "batch_id": batch_num,
                    "expected_value": round(pre_drift_fee_rate, 6),
                    "actual_value": round(post_drift_fee_rate, 6),
                    "description": (
                        f"Batch {batch_num}: fee_rate is {post_drift_fee_rate:.2%} "
                        f"(Rs.{batch_total_fees:.2f} fees on Rs.{batch_total_gross:.2f} gross) vs. "
                        f"an undrifted {pre_drift_fee_rate:.2%} -- every entry's fee was uniformly "
                        f"inflated by apply_fee_drift(), visible only as a batch-wide statistical "
                        f"outlier, not any single order-level mismatch."
                    ),
                }
            )

        batch_total_gross = round2(batch_total_gross)
        batch_total_fees = round2(batch_total_fees)
        batch_total_tax = round2(batch_total_tax)
        batch_total_refunds = round2(batch_total_refunds)
        batch_total_net = round2(batch_total_net)

        bank_date = cfg["settlement_date"]
        if cfg["timing_mismatch"]:
            bank_date = bank_date + datetime.timedelta(days=2)
            ground_truth.append(
                {
                    "type": "timing_mismatch",
                    "order_ref": None,
                    "batch_id": batch_num,
                    "expected_value": cfg["settlement_date"].isoformat(),
                    "actual_value": bank_date.isoformat(),
                    "description": (
                        f"Batch {batch_num}: settlement_date is {cfg['settlement_date'].isoformat()} "
                        f"but the matching bank credit is dated {bank_date.isoformat()} "
                        f"(2 days later)."
                    ),
                }
            )

        # Real Razorpay settlement narrations don't carry a batch/settlement ID --
        # every payout from the same merchant account shows the same fixed bank/
        # gateway text, varying only by UTR. Reusing bank_reference here (not a
        # second make_bank_reference() call) keeps the UTR in the narration
        # identical to the UTR in the reference column, as a real statement line
        # would be, and consumes no additional random draws.
        bank_reference = make_bank_reference()
        bank_rows.append(
            {
                "batch_num": batch_num,
                "date": bank_date,
                "amount": batch_total_net,
                "reference": bank_reference,
                "description": f"NEFT CR: HDFC BANK {bank_reference} RAZORPAY SETTLEMENT",
            }
        )

        batches.append(
            {
                "batch_num": batch_num,
                "settlement_date": cfg["settlement_date"],
                "total_gross": batch_total_gross,
                "total_refunds": batch_total_refunds,
                "total_fees": batch_total_fees,
                "total_tax": batch_total_tax,
                "total_net": batch_total_net,
            }
        )

    # Noise: realistic-looking bank transactions that are NOT Razorpay settlement
    # credits and are not linked to any SettlementBatch. Fixed/hardcoded, not drawn
    # from random() -- added after the per-batch loop above so they cannot shift
    # that loop's random-stream position, and their amounts (a few thousand rupees)
    # are far outside matching.AMOUNT_TOLERANCE of every batch's total_net (all
    # >Rs.90,000), so the deterministic matcher leaves them unmatched by construction,
    # not by luck. batch_num=None: these were never part of any batch -- the field
    # itself is otherwise unused by anything downstream of this script.
    bank_rows.append(
        {
            "batch_num": None,
            "date": datetime.date(2026, 9, 5),
            "amount": 4250.00,
            "reference": "NEFTN52847193028",
            "description": "NEFT DR: VENDOR PAYMENT - OFFICE SUPPLIES",
        }
    )
    bank_rows.append(
        {
            "batch_num": None,
            "date": datetime.date(2026, 9, 18),
            "amount": 1200.00,
            "reference": "UPI488273649102",
            "description": "UPI CR: CUSTOMER REFUND REVERSAL",
        }
    )
    bank_rows.append(
        {
            "batch_num": None,
            "date": datetime.date(2026, 10, 12),
            "amount": 850.00,
            "reference": "NEFTN71923847561",
            "description": "NEFT DR: BANK CHARGES - ANNUAL MAINTENANCE",
        }
    )

    return batches, settlement_rows, order_rows, bank_rows, ground_truth


def write_csvs(batches, settlement_rows, order_rows, bank_rows):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    batches_path = DATA_DIR / "settlement_batches.csv"
    with open(batches_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "batch_id",
                "settlement_date",
                "total_gross",
                "total_refunds",
                "total_fees",
                "total_tax",
                "total_net",
            ],
        )
        writer.writeheader()
        for batch in batches:
            writer.writerow(
                {
                    "batch_id": batch["batch_num"],
                    "settlement_date": batch["settlement_date"].isoformat(),
                    "total_gross": batch["total_gross"],
                    "total_refunds": batch["total_refunds"],
                    "total_fees": batch["total_fees"],
                    "total_tax": batch["total_tax"],
                    "total_net": batch["total_net"],
                }
            )

    settlement_path = DATA_DIR / "razorpay_settlement.csv"
    with open(settlement_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "batch_id",
                "settlement_date",
                "order_ref",
                "gross_amount",
                "fee",
                "tax",
                "refund",
                "net_amount",
            ],
        )
        writer.writeheader()
        settlement_date_by_batch = {}
        for row in settlement_rows:
            writer.writerow(
                {
                    "batch_id": row["batch_num"],
                    "settlement_date": settlement_date_by_batch.setdefault(
                        row["batch_num"],
                        next(b["settlement_date"] for b in BATCH_CONFIGS if b["batch_num"] == row["batch_num"]),
                    ).isoformat(),
                    "order_ref": row["order_ref"],
                    "gross_amount": row["gross_amount"],
                    "fee": row["fee"],
                    "tax": row["tax"],
                    "refund": row["refund"],
                    "net_amount": row["net_amount"],
                }
            )

    bank_path = DATA_DIR / "bank_statement.csv"
    with open(bank_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "amount", "reference", "description"])
        writer.writeheader()
        for row in bank_rows:
            writer.writerow(
                {
                    "date": row["date"].isoformat(),
                    "amount": row["amount"],
                    "reference": row["reference"],
                    "description": row["description"],
                }
            )

    orders_path = DATA_DIR / "order_records.csv"
    with open(orders_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["order_ref", "amount", "status", "refund_amount", "fee_amount"]
        )
        writer.writeheader()
        for row in order_rows:
            writer.writerow(row)

    return batches_path, settlement_path, bank_path, orders_path


def write_ground_truth(ground_truth):
    path = DATA_DIR / "ground_truth.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ground_truth, f, indent=2)
    return path


def main():
    batches, settlement_rows, order_rows, bank_rows, ground_truth = build_dataset()

    batches_path, settlement_path, bank_path, orders_path = write_csvs(
        batches, settlement_rows, order_rows, bank_rows
    )
    ground_truth_path = write_ground_truth(ground_truth)

    print("Synthetic data generation complete.")
    print()
    print("CSV exports:")
    print(f"  {batches_path} ({len(batches)} rows)")
    print(f"  {settlement_path} ({len(settlement_rows)} rows)")
    print(f"  {bank_path} ({len(bank_rows)} rows)")
    print(f"  {orders_path} ({len(order_rows)} rows)")
    print()
    print(f"Ground truth: {ground_truth_path} ({len(ground_truth)} injected errors)")
    for entry in ground_truth:
        print(f"  [{entry['type']}] batch {entry['batch_id']} order {entry['order_ref']}")


if __name__ == "__main__":
    main()
