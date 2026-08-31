"""
Isolated-DB harness for the Phase D hero case. Same isolation discipline as
app/holdout_evaluation.py: a fresh in-memory SQLite database created here,
never app.database.engine/SessionLocal, seeded with
scripts/generate_hero_case_data.py's dataset, then run through the REAL
matching/bridge/exceptions pipeline (imported and called unmodified) to get a
genuine exception_id -- not a hand-assigned fake one -- ready to hand to
app.investigation_agent.investigate_exception().
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import bridge, exceptions, matching, models
from app.database import Base
from scripts.generate_hero_case_data import build_hero_case_dataset


def build_hero_case_session():
    """Returns (db, batch_id, missing_refund_exception_id, timing_exception_id_or_None).
    The caller is responsible for closing db when done."""
    dataset = build_hero_case_dataset()

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    txn = models.BankTransaction(
        amount=dataset["bank_txn"]["amount"], date=dataset["bank_txn"]["date"],
        reference=dataset["bank_txn"]["reference"], description=dataset["bank_txn"]["description"],
    )
    db.add(txn)
    db.flush()

    b = dataset["batch"]
    batch = models.SettlementBatch(
        settlement_date=b["settlement_date"], total_gross=b["total_gross"],
        total_refunds=b["total_refunds"], total_fees=b["total_fees"],
        total_tax=b["total_tax"], total_net=b["total_net"],
    )
    db.add(batch)
    db.flush()

    for e in dataset["entries"]:
        db.add(models.SettlementEntry(
            batch_id=batch.id, order_ref=e["order_ref"], gross_amount=e["gross_amount"],
            fee=e["fee"], tax=e["tax"], refund=e["refund"], net_amount=e["net_amount"],
        ))
    for o in dataset["orders"]:
        db.add(models.OrderRecord(
            order_ref=o["order_ref"], amount=o["amount"], status=o["status"],
            refund_amount=o["refund_amount"], fee_amount=o["fee_amount"],
        ))
    db.commit()

    matching.run_matching(db)
    bridge.compute_bridge(db)
    exceptions.classify_exceptions(db)

    missing_refund_exc = (
        db.query(models.ExceptionRecord)
        .filter(models.ExceptionRecord.batch_id == batch.id,
                 models.ExceptionRecord.classification == "MISSING_REFUND_RECORD")
        .first()
    )
    timing_exc = (
        db.query(models.ExceptionRecord)
        .filter(models.ExceptionRecord.batch_id == batch.id,
                 models.ExceptionRecord.classification == "TIMING_DIFFERENCE")
        .first()
    )

    return db, batch.id, (missing_refund_exc.id if missing_refund_exc else None), (
        timing_exc.id if timing_exc else None
    )
