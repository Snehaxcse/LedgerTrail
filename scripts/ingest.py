"""
Ingests the four raw CSV exports (data/settlement_batches.csv, data/razorpay_settlement.csv,
data/bank_statement.csv, data/order_records.csv) into the database tables.

This is ingestion only: rows are parsed, validated, and loaded as-is. No matching,
no bridge calculation, no exception detection. SettlementBatch totals are loaded
directly from settlement_batches.csv's own declared columns -- ingestion does not
derive them by summing SettlementEntry rows, since that would silently double-count
any duplicated line item and make the bridge's internal-consistency check meaningless.
Bank transactions are loaded standalone with no batch link, since deciding which
bank credit belongs to which batch is matching logic and comes later.
"""
import csv
import datetime
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import Base, engine, SessionLocal
from app import models

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

BATCH_REQUIRED = ["batch_id", "settlement_date", "total_gross", "total_refunds", "total_fees", "total_tax", "total_net"]
SETTLEMENT_REQUIRED = ["batch_id", "settlement_date", "order_ref", "gross_amount", "fee", "tax", "refund", "net_amount"]
BANK_REQUIRED = ["date", "amount", "reference"]
ORDER_REQUIRED = ["order_ref", "amount", "status", "fee_amount"]

PAISE_PER_RUPEE = 100


def parse_rupees_to_paise(value):
    """Converts a decimal-rupee CSV string into integer paise. Every currency field
    ingest.py parses is a rupee amount destined for a now-Integer (paise) column --
    see float-to-paise migration Phase 1 in app/models.py. Uses decimal.Decimal for
    both the parse and the *100 multiplication, not float, so the migration doesn't
    reintroduce the exact binary-float imprecision it exists to eliminate (e.g.
    float(116294.35) * 100 is not exactly 11629435 in IEEE 754; Decimal("116294.35")
    * 100 is exactly Decimal("11629435.00")). ROUND_HALF_UP (not Python's
    round-half-to-even default) for the conventional "round half up" behavior
    expected of money."""
    try:
        decimal_value = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"invalid decimal value: {value!r}")
    paise = (decimal_value * PAISE_PER_RUPEE).to_integral_value(rounding=ROUND_HALF_UP)
    return int(paise)


def parse_date(value):
    return datetime.date.fromisoformat(value)


def missing_fields(row, required):
    return [f for f in required if row.get(f) is None or row.get(f).strip() == ""]


def read_csv_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def ingest_batches(path):
    """Returns (valid_rows, rejects). Batch totals are read as-is, never derived."""
    valid_rows = []
    rejects = []

    for i, row in enumerate(read_csv_rows(path), start=2):
        missing = missing_fields(row, BATCH_REQUIRED)
        if missing:
            rejects.append((i, row, f"missing required field(s): {', '.join(missing)}"))
            continue
        try:
            valid_rows.append(
                {
                    "batch_id": int(row["batch_id"]),
                    "settlement_date": parse_date(row["settlement_date"]),
                    "total_gross": parse_rupees_to_paise(row["total_gross"]),
                    "total_refunds": parse_rupees_to_paise(row["total_refunds"]),
                    "total_fees": parse_rupees_to_paise(row["total_fees"]),
                    "total_tax": parse_rupees_to_paise(row["total_tax"]),
                    "total_net": parse_rupees_to_paise(row["total_net"]),
                }
            )
        except ValueError as e:
            rejects.append((i, row, f"unparseable value: {e}"))

    return valid_rows, rejects


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
                    "gross_amount": parse_rupees_to_paise(row["gross_amount"]),
                    "fee": parse_rupees_to_paise(row["fee"]),
                    "tax": parse_rupees_to_paise(row["tax"]),
                    "refund": parse_rupees_to_paise(row["refund"]),
                    "net_amount": parse_rupees_to_paise(row["net_amount"]),
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
                    "amount": parse_rupees_to_paise(row["amount"]),
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
                    "amount": parse_rupees_to_paise(row["amount"]),
                    "status": row["status"].strip(),
                    "refund_amount": parse_rupees_to_paise(refund_raw) if refund_raw != "" else None,
                    "fee_amount": parse_rupees_to_paise(row["fee_amount"]),
                }
            )
        except ValueError as e:
            rejects.append((i, row, f"unparseable value: {e}"))

    return valid_rows, rejects


def load_into_db(batch_rows, settlement_rows, bank_rows, order_rows):
    """Returns (counts, unresolved_settlement_rows) -- the latter are settlement rows
    whose batch_id doesn't match any row in settlement_batches.csv, so they're skipped."""
    Base.metadata.create_all(engine)

    db = SessionLocal()
    try:
        # AuditEvent is deliberately excluded from this wipe: it is an append-only
        # audit log and must never be updated or deleted, even on re-ingestion.
        # IngestedEvent is wiped for the same reason ApprovalLog is: it FK-references
        # SettlementBatch ids, which are recreated (reassigned) by this same function --
        # leaving stale rows here would let a live-ingested demo batch's IngestedEvent
        # row survive a regen while the batch it points to is gone, permanently stuck
        # reporting "duplicate" for a batch that no longer exists.
        db.query(models.IngestedEvent).delete()
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

        batch_pk_by_csv_id = {}
        for row in batch_rows:
            batch = models.SettlementBatch(
                settlement_date=row["settlement_date"],
                total_gross=row["total_gross"],
                total_refunds=row["total_refunds"],
                total_fees=row["total_fees"],
                total_tax=row["total_tax"],
                total_net=row["total_net"],
                bank_transaction_id=None,  # linking to a bank credit is matching logic, not ingestion
            )
            db.add(batch)
            db.flush()
            batch_pk_by_csv_id[row["batch_id"]] = batch.id
        db.commit()

        unresolved_settlement_rows = []
        for row in settlement_rows:
            batch_pk = batch_pk_by_csv_id.get(row["batch_id"])
            if batch_pk is None:
                unresolved_settlement_rows.append(row)
                continue
            db.add(
                models.SettlementEntry(
                    batch_id=batch_pk,
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
        return counts, unresolved_settlement_rows
    finally:
        db.close()


def print_rejects(label, rejects):
    for line_no, row, reason in rejects:
        print(f"  [{label}] line {line_no} REJECTED: {reason} -- raw row: {row}")


def main():
    batches_path = DATA_DIR / "settlement_batches.csv"
    settlement_path = DATA_DIR / "razorpay_settlement.csv"
    bank_path = DATA_DIR / "bank_statement.csv"
    orders_path = DATA_DIR / "order_records.csv"

    batch_rows, batch_rejects = ingest_batches(batches_path)
    settlement_rows, settlement_rejects = ingest_settlement(settlement_path)
    bank_rows, bank_rejects = ingest_bank(bank_path)
    order_rows, order_rejects = ingest_orders(orders_path)

    print("Validation:")
    print_rejects("batches", batch_rejects)
    print_rejects("settlement", settlement_rejects)
    print_rejects("bank", bank_rejects)
    print_rejects("orders", order_rejects)
    if not (batch_rejects or settlement_rejects or bank_rejects or order_rejects):
        print("  no rejected rows")
    print()

    counts, unresolved_settlement_rows = load_into_db(batch_rows, settlement_rows, bank_rows, order_rows)
    for row in unresolved_settlement_rows:
        print(f"  [settlement] REJECTED after load: unknown batch_id={row['batch_id']} -- order_ref={row['order_ref']}")

    ingested_settlement_count = len(settlement_rows) - len(unresolved_settlement_rows)

    print("Ingestion summary:")
    print(f"  settlement_batches.csv:  {len(batch_rows)} ingested, {len(batch_rejects)} rejected")
    print(
        f"  razorpay_settlement.csv: {ingested_settlement_count} ingested, "
        f"{len(settlement_rejects) + len(unresolved_settlement_rows)} rejected"
    )
    print(f"  bank_statement.csv:      {len(bank_rows)} ingested, {len(bank_rejects)} rejected")
    print(f"  order_records.csv:       {len(order_rows)} ingested, {len(order_rejects)} rejected")
    print()
    print("Database row counts after ingestion:")
    for k, v in counts.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
