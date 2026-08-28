"""
Ingests the three raw CSV exports (data/razorpay_settlement.csv, data/bank_statement.csv,
data/order_records.csv) into the database tables.

This is ingestion only: rows are parsed, validated, and loaded as-is. No matching,
no bridge calculation, no exception detection. The only "computation" here is summing
each settlement batch's own line items into its SettlementBatch totals row -- plain
deterministic aggregation of the batch's own numbers, not reconciliation against
anything else. Bank transactions are loaded standalone with no batch link, since
deciding which bank credit belongs to which batch is matching logic and comes later.
"""
import csv
import datetime
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import Base, engine, SessionLocal
from app import models

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

SETTLEMENT_REQUIRED = ["batch_id", "settlement_date", "order_ref", "gross_amount", "fee", "tax", "refund", "net_amount"]
BANK_REQUIRED = ["date", "amount", "reference"]
ORDER_REQUIRED = ["order_ref", "amount", "status", "fee_amount"]


def parse_float(value):
    return float(value)


def parse_date(value):
    return datetime.date.fromisoformat(value)


def missing_fields(row, required):
    return [f for f in required if row.get(f) is None or row.get(f).strip() == ""]


def read_csv_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def ingest_settlement(path):
    """Returns (valid_rows, rejects) where valid_rows have parsed types."""
    valid_rows = []
    rejects = []

    for i, row in enumerate(read_csv_rows(path), start=2):  # start=2: header is line 1
        missing = missing_fields(row, SETTLEMENT_REQUIRED)
        if missing:
            rejects.append((i, row, f"missing required field(s): {', '.join(missing)}"))
            continue
        try:
            valid_rows.append(
                {
                    "batch_id": int(row["batch_id"]),
                    "settlement_date": parse_date(row["settlement_date"]),
                    "order_ref": row["order_ref"].strip(),
                    "gross_amount": parse_float(row["gross_amount"]),
                    "fee": parse_float(row["fee"]),
                    "tax": parse_float(row["tax"]),
                    "refund": parse_float(row["refund"]),
                    "net_amount": parse_float(row["net_amount"]),
                }
            )
        except ValueError as e:
            rejects.append((i, row, f"unparseable value: {e}"))

    return valid_rows, rejects


def ingest_bank(path):
    valid_rows = []
    rejects = []

    for i, row in enumerate(read_csv_rows(path), start=2):
        missing = missing_fields(row, BANK_REQUIRED)
        if missing:
            rejects.append((i, row, f"missing required field(s): {', '.join(missing)}"))
            continue
        try:
            valid_rows.append(
                {
                    "date": parse_date(row["date"]),
                    "amount": parse_float(row["amount"]),
                    "reference": row["reference"].strip(),
                    "description": (row.get("description") or "").strip() or None,
                }
            )
        except ValueError as e:
            rejects.append((i, row, f"unparseable value: {e}"))

    return valid_rows, rejects


def ingest_orders(path):
    valid_rows = []
    rejects = []

    for i, row in enumerate(read_csv_rows(path), start=2):
        missing = missing_fields(row, ORDER_REQUIRED)
        if missing:
            rejects.append((i, row, f"missing required field(s): {', '.join(missing)}"))
            continue
        try:
            refund_raw = (row.get("refund_amount") or "").strip()
            valid_rows.append(
                {
                    "order_ref": row["order_ref"].strip(),
                    "amount": parse_float(row["amount"]),
                    "status": row["status"].strip(),
                    "refund_amount": parse_float(refund_raw) if refund_raw != "" else None,
                    "fee_amount": parse_float(row["fee_amount"]),
                }
            )
        except ValueError as e:
            rejects.append((i, row, f"unparseable value: {e}"))

    return valid_rows, rejects


def load_into_db(settlement_rows, bank_rows, order_rows):
    Base.metadata.create_all(engine)

    db = SessionLocal()
    try:
        db.query(models.AuditEvent).delete()
        db.query(models.ApprovalLog).delete()
        db.query(models.ExceptionRecord).delete()
        db.query(models.Match).delete()
        db.query(models.OrderRecord).delete()
        db.query(models.SettlementEntry).delete()
        db.query(models.SettlementBatch).delete()
        db.query(models.BankTransaction).delete()
        db.commit()

        for row in bank_rows:
            db.add(
                models.BankTransaction(
                    amount=row["amount"],
                    date=row["date"],
                    reference=row["reference"],
                    description=row["description"],
                )
            )
        db.commit()

        groups = defaultdict(list)
        for row in settlement_rows:
            groups[(row["batch_id"], row["settlement_date"])].append(row)

        batch_pk_by_key = {}
        for (csv_batch_id, settlement_date), rows in groups.items():
            batch = models.SettlementBatch(
                settlement_date=settlement_date,
                total_gross=round(sum(r["gross_amount"] for r in rows), 2),
                total_refunds=round(sum(r["refund"] for r in rows), 2),
                total_fees=round(sum(r["fee"] for r in rows), 2),
                total_tax=round(sum(r["tax"] for r in rows), 2),
                total_net=round(sum(r["net_amount"] for r in rows), 2),
                bank_transaction_id=None,  # linking to a bank credit is matching logic, not ingestion
            )
            db.add(batch)
            db.flush()
            batch_pk_by_key[(csv_batch_id, settlement_date)] = batch.id
        db.commit()

        for row in settlement_rows:
            db.add(
                models.SettlementEntry(
                    batch_id=batch_pk_by_key[(row["batch_id"], row["settlement_date"])],
                    order_ref=row["order_ref"],
                    gross_amount=row["gross_amount"],
                    fee=row["fee"],
                    tax=row["tax"],
                    refund=row["refund"],
                    net_amount=row["net_amount"],
                )
            )
        db.commit()

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


def print_rejects(label, rejects):
    for line_no, row, reason in rejects:
        print(f"  [{label}] line {line_no} REJECTED: {reason} -- raw row: {row}")


def main():
    settlement_path = DATA_DIR / "razorpay_settlement.csv"
    bank_path = DATA_DIR / "bank_statement.csv"
    orders_path = DATA_DIR / "order_records.csv"

    settlement_rows, settlement_rejects = ingest_settlement(settlement_path)
    bank_rows, bank_rejects = ingest_bank(bank_path)
    order_rows, order_rejects = ingest_orders(orders_path)

    print("Validation:")
    print_rejects("settlement", settlement_rejects)
    print_rejects("bank", bank_rejects)
    print_rejects("orders", order_rejects)
    if not (settlement_rejects or bank_rejects or order_rejects):
        print("  no rejected rows")
    print()

    counts = load_into_db(settlement_rows, bank_rows, order_rows)

    print("Ingestion summary:")
    print(f"  razorpay_settlement.csv: {len(settlement_rows)} ingested, {len(settlement_rejects)} rejected")
    print(f"  bank_statement.csv:      {len(bank_rows)} ingested, {len(bank_rejects)} rejected")
    print(f"  order_records.csv:       {len(order_rows)} ingested, {len(order_rejects)} rejected")
    print()
    print("Database row counts after ingestion:")
    for k, v in counts.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
