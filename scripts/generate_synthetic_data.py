"""
Generates synthetic Razorpay-style settlement reconciliation data for LedgerTrail (Day 1).

Writes rows into the SQLite database via the SQLAlchemy models, exports the same
source data as three raw CSVs (data/razorpay_settlement.csv, data/bank_statement.csv,
data/order_records.csv), and writes data/ground_truth.json documenting every
error deliberately injected into the dataset.

No matching, bridge, or exception logic lives here -- this script only produces
raw source data and the answer key for it. All math here is plain deterministic
Python (fee tiers, GST, rounding), never an LLM.
"""
import csv
import datetime
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import Base, engine, SessionLocal
from app import models

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
    {"batch_num": 9, "settlement_date": datetime.date(2026, 10, 22), "num_entries": 15, "timing_mismatch": False},
    {"batch_num": 10, "settlement_date": datetime.date(2026, 11, 5), "num_entries": 18, "timing_mismatch": False},
]

# Exactly one occurrence of each injected error type, placed by (batch_num, index in batch).
# Each batch carries exactly one error type (batch 1 is left completely clean on
# purpose) so a single-classification-per-batch reconciliation engine can still be
# exercised against every error type in isolation.
INJECTED_ROLES = {
    (2, 0): "missing_refund",
    (2, 1): "wrong_fee_tier",
    (3, 0): "duplicate_entry",
}


def round2(x):
    return round(x, 2)


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

        bank_rows.append(
            {
                "batch_num": batch_num,
                "date": bank_date,
                "amount": batch_total_net,
                "reference": make_bank_reference(),
                "description": f"NEFT CR-RAZORPAY SETTLEMENT-BATCH{batch_num}",
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

    return batches, settlement_rows, order_rows, bank_rows, ground_truth


def write_to_db(batches, settlement_rows, order_rows, bank_rows):
    Base.metadata.create_all(engine)

    db = SessionLocal()
    try:
        # Wipe existing data so the script is safely re-runnable. AuditEvent is
        # deliberately excluded: it is an append-only audit log and must never be
        # updated or deleted, even when the rest of the dataset is regenerated.
        db.query(models.ApprovalLog).delete()
        db.query(models.ExceptionRecord).delete()
        db.query(models.Match).delete()
        db.query(models.OrderRecord).delete()
        db.query(models.SettlementEntry).delete()
        db.query(models.SettlementBatch).delete()
        db.query(models.BankTransaction).delete()
        db.commit()

        bank_txn_by_batch = {}
        for row in bank_rows:
            txn = models.BankTransaction(
                amount=row["amount"],
                date=row["date"],
                reference=row["reference"],
                description=row["description"],
            )
            db.add(txn)
            db.flush()
            bank_txn_by_batch[row["batch_num"]] = txn.id

        batch_id_by_num = {}
        for batch in batches:
            b = models.SettlementBatch(
                settlement_date=batch["settlement_date"],
                total_gross=batch["total_gross"],
                total_refunds=batch["total_refunds"],
                total_fees=batch["total_fees"],
                total_tax=batch["total_tax"],
                total_net=batch["total_net"],
                bank_transaction_id=bank_txn_by_batch[batch["batch_num"]],
            )
            db.add(b)
            db.flush()
            batch_id_by_num[batch["batch_num"]] = b.id

        for row in settlement_rows:
            db.add(
                models.SettlementEntry(
                    batch_id=batch_id_by_num[row["batch_num"]],
                    order_ref=row["order_ref"],
                    gross_amount=row["gross_amount"],
                    fee=row["fee"],
                    tax=row["tax"],
                    refund=row["refund"],
                    net_amount=row["net_amount"],
                )
            )

        for row in order_rows:
            db.add(
                models.OrderRecord(
                    order_ref=row["order_ref"],
                    amount=row["amount"],
                    status=row["status"],
                    refund_amount=row["refund_amount"],
                    fee_amount=row["fee_amount"],
                )
            )

        db.commit()

        counts = {
            "bank_transactions": db.query(models.BankTransaction).count(),
            "settlement_batches": db.query(models.SettlementBatch).count(),
            "settlement_entries": db.query(models.SettlementEntry).count(),
            "order_records": db.query(models.OrderRecord).count(),
        }
        return counts
    finally:
        db.close()


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

    db_counts = write_to_db(batches, settlement_rows, order_rows, bank_rows)
    batches_path, settlement_path, bank_path, orders_path = write_csvs(
        batches, settlement_rows, order_rows, bank_rows
    )
    ground_truth_path = write_ground_truth(ground_truth)

    print("Synthetic data generation complete.")
    print()
    print("Database rows:")
    for k, v in db_counts.items():
        print(f"  {k}: {v}")
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
