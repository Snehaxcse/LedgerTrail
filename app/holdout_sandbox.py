"""
Live, interactive demo features built on the held-out dataset
(scripts/generate_holdout_data.py) -- idempotent-replay, step-by-step
reconciliation progress, and a real approval-race demonstration. Same
isolation discipline as app/holdout_evaluation.py: every database here is a
fresh sqlite:///:memory: engine, created in this module and only this
module, never app.database.engine/SessionLocal. The real ledgertrail.db is
structurally unreachable from anything in this file.

Distinct from app/holdout_evaluation.py's own pattern, though: that module is
stateless -- one call, one throwaway database, done. The three features here
need a database that survives ACROSS multiple HTTP requests within one demo
flow (e.g. run matching in one request, bridge in the next; approve as Sneha
in one request, attempt to approve as Rahul in a second request against the
SAME row). _SANDBOXES is a simple in-memory registry keyed by a random
sandbox_id, holding a live engine+session-factory per sandbox, evicted after
SANDBOX_TTL_SECONDS of inactivity so a long-running demo server doesn't
accumulate abandoned sandboxes forever. This is fine for a single-instance
demo deployment; it would need a shared store (Redis, a table with a TTL
column) behind multiple worker processes, which this project has never run.
"""
import datetime
import time
import uuid
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import anomaly_detection, bridge, exceptions, matching, models
from app.database import Base
from app.exceptions import CLASSIFICATION_INFO
from app.money import paise_to_rupees
from scripts.generate_holdout_data import build_holdout_dataset

SANDBOX_TTL_SECONDS = 30 * 60

_sandboxes: Dict[str, Dict[str, Any]] = {}
_lock = Lock()


def _new_isolated_engine():
    # poolclass=StaticPool is not optional here: SQLAlchemy's default pooling
    # for sqlite:///:memory: can hand out a DIFFERENT underlying connection
    # (and therefore a DIFFERENT, empty in-memory database) to each new
    # Session() call. Every other isolated-DB harness in this codebase
    # (app/holdout_evaluation.py, app/hero_case.py, ...) creates exactly one
    # Session per engine and never hits this, since the gotcha only shows up
    # across multiple, separately-created sessions against the same engine --
    # which is exactly what this module's cross-request sandboxes do.
    # StaticPool guarantees every session, request after request, shares the
    # one real connection and therefore the one real in-memory database.
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def _evict_expired_locked():
    now = time.time()
    expired = [sid for sid, entry in _sandboxes.items() if now - entry["created_at"] > SANDBOX_TTL_SECONDS]
    for sid in expired:
        del _sandboxes[sid]


def create_sandbox() -> str:
    """Registers a fresh isolated in-memory database and returns its id.
    Caller is responsible for seeding it (see seed_raw_records below)."""
    engine = _new_isolated_engine()
    session_factory = sessionmaker(bind=engine)
    sandbox_id = uuid.uuid4().hex
    with _lock:
        _evict_expired_locked()
        _sandboxes[sandbox_id] = {
            "session_factory": session_factory,
            "created_at": time.time(),
            "batch_id_by_label": {},
            "ambiguous_labels": set(),
        }
    return sandbox_id


def get_sandbox_session(sandbox_id: str):
    """Returns a fresh Session bound to this sandbox's engine, or None if the
    sandbox_id is unknown/expired. Caller must close the session."""
    with _lock:
        entry = _sandboxes.get(sandbox_id)
    if entry is None:
        return None
    return entry["session_factory"]()


def _sandbox_entry(sandbox_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        return _sandboxes.get(sandbox_id)


# --- Feature: step-by-step reconciliation progress --------------------------

def seed_raw_records(db, sandbox_id: str) -> int:
    """Step 1: loads the held-out dataset's raw batches/entries/orders/bank
    transactions -- no matching/classification yet. Returns the number of
    batches loaded (== "records" throughout this module, matching the 14
    ground-truth cases in scripts/generate_holdout_data.py)."""
    dataset = build_holdout_dataset()
    entry = _sandbox_entry(sandbox_id)

    bank_id_by_label = {}
    for row in dataset["bank_txns"]:
        txn = models.BankTransaction(
            amount=row["amount"], date=row["date"], reference=row["reference"], description=row["description"],
        )
        db.add(txn)
        db.flush()
        bank_id_by_label[row["label"]] = txn.id

    batch_id_by_label = {}
    for row in dataset["batches"]:
        b = models.SettlementBatch(
            settlement_date=row["settlement_date"], total_gross=row["total_gross"],
            total_refunds=row["total_refunds"], total_fees=row["total_fees"],
            total_tax=row["total_tax"], total_net=row["total_net"],
        )
        db.add(b)
        db.flush()
        batch_id_by_label[row["label"]] = b.id

    for row in dataset["entries"]:
        db.add(models.SettlementEntry(
            batch_id=batch_id_by_label[row["batch_label"]], order_ref=row["order_ref"],
            gross_amount=row["gross_amount"], fee=row["fee"], tax=row["tax"],
            refund=row["refund"], net_amount=row["net_amount"],
        ))

    for row in dataset["orders"]:
        db.add(models.OrderRecord(
            order_ref=row["order_ref"], amount=row["amount"], status=row["status"],
            refund_amount=row["refund_amount"], fee_amount=row["fee_amount"],
        ))

    db.commit()

    entry["batch_id_by_label"] = batch_id_by_label
    entry["ambiguous_source_labels"] = dataset["ambiguous_batch_labels"]
    return len(dataset["batches"])


def run_matching_step(db, sandbox_id: str) -> Dict[str, int]:
    """Step 2: the real matching.run_matching(), unmodified. Handles the
    dataset's deliberately-planted AmbiguousMatchError exactly as
    app/holdout_evaluation.py does (excludes the known ambiguous pair and
    retries once) -- this changes what DATA the real function sees, never
    what the function itself does."""
    entry = _sandbox_entry(sandbox_id)
    batch_id_by_label = entry["batch_id_by_label"]
    ambiguous_source_labels = entry.get("ambiguous_source_labels", set())

    try:
        matching.run_matching(db)
        ambiguous_labels = set()
    except matching.AmbiguousMatchError:
        excluded_ids = {batch_id_by_label[label] for label in ambiguous_source_labels}
        for batch_id in excluded_ids:
            b = db.query(models.SettlementBatch).filter(models.SettlementBatch.id == batch_id).first()
            if b.bank_transaction_id is not None:
                db.query(models.BankTransaction).filter(models.BankTransaction.id == b.bank_transaction_id).delete()
        db.query(models.SettlementBatch).filter(models.SettlementBatch.id.in_(excluded_ids)).delete(
            synchronize_session=False
        )
        db.commit()
        matching.run_matching(db)
        ambiguous_labels = ambiguous_source_labels

    with _lock:
        _sandboxes[sandbox_id]["ambiguous_labels"] = ambiguous_labels

    matched_count = (
        db.query(models.SettlementBatch).filter(models.SettlementBatch.bank_transaction_id.isnot(None)).count()
    )
    return {"matched_count": matched_count, "ambiguous_excluded": len(ambiguous_labels)}


def run_bridge_step(db) -> Dict[str, int]:
    """Step 3: the real bridge.compute_bridge(), unmodified."""
    rows = bridge.compute_bridge(db)
    return {"bridges_calculated": len(rows)}


def run_classification_step(db) -> Dict[str, int]:
    """Step 4: the real exceptions.classify_exceptions() and
    anomaly_detection.run_anomaly_detection(), unmodified -- reported as one
    step's worth of sub-facts (duplicates found, total requiring review)
    since both counts come from data that's genuinely all present the moment
    this one call returns, not staggered fake progress."""
    exceptions.classify_exceptions(db)
    anomaly_detection.run_anomaly_detection(db)

    all_exceptions = db.query(models.ExceptionRecord).all()
    duplicates = sum(1 for e in all_exceptions if e.classification == "DUPLICATE_ENTRY")
    requires_review = sum(
        1 for e in all_exceptions
        if CLASSIFICATION_INFO.get(e.classification, {}).get("requires_approval", True)
    )
    return {
        "total_exceptions": len(all_exceptions),
        "duplicates_detected": duplicates,
        "requires_review": requires_review,
    }


# --- Feature: idempotent ingestion replay ------------------------------------

@dataclass
class IngestionPassResult:
    records_seen: int
    accepted: int
    duplicates: int


def _ingest_one_batch_idempotent(db, batch_row, entries_for_batch, orders_by_ref, bank_row) -> bool:
    """One 'record' = one held-out batch (with its settlement entries, order
    records, and -- unless bank_row is None -- a bank transaction). Attempts
    to insert a HeldOutIngestionRecord with a source_event_id derived from
    the batch's own label BEFORE inserting any of its actual data. Returns
    True if this was a genuinely new record (data inserted), False if
    source_event_id already existed -- decided by a real IntegrityError on
    the UNIQUE constraint (app/models.py), not by checking for existence
    first, which a caller could route around.

    bank_row is None for "10b" specifically: the ambiguous pair ("10a"/"10b")
    shares ONE real bank credit (that's the case's whole point), created once
    when "10a" is processed -- see run_ingestion_pass."""
    source_event_id = f"holdout-batch-{batch_row['label']}"
    try:
        db.add(models.HeldOutIngestionRecord(
            source_event_id=source_event_id, ingested_at=datetime.datetime.now(),
        ))
        db.flush()
    except IntegrityError:
        db.rollback()
        return False

    if bank_row is not None:
        txn = models.BankTransaction(
            amount=bank_row["amount"], date=bank_row["date"],
            reference=bank_row["reference"], description=bank_row["description"],
        )
        db.add(txn)
        db.flush()

    b = models.SettlementBatch(
        settlement_date=batch_row["settlement_date"], total_gross=batch_row["total_gross"],
        total_refunds=batch_row["total_refunds"], total_fees=batch_row["total_fees"],
        total_tax=batch_row["total_tax"], total_net=batch_row["total_net"],
    )
    db.add(b)
    db.flush()

    for row in entries_for_batch:
        db.add(models.SettlementEntry(
            batch_id=b.id, order_ref=row["order_ref"], gross_amount=row["gross_amount"],
            fee=row["fee"], tax=row["tax"], refund=row["refund"], net_amount=row["net_amount"],
        ))
    for row in entries_for_batch:
        order = orders_by_ref.get(row["order_ref"])
        if order is not None:
            db.add(models.OrderRecord(
                order_ref=order["order_ref"], amount=order["amount"], status=order["status"],
                refund_amount=order["refund_amount"], fee_amount=order["fee_amount"],
            ))

    db.commit()
    return True


def run_ingestion_pass(db, dataset) -> IngestionPassResult:
    entries_by_batch: Dict[str, List[dict]] = {}
    for row in dataset["entries"]:
        entries_by_batch.setdefault(row["batch_label"], []).append(row)
    orders_by_ref = {row["order_ref"]: row for row in dataset["orders"]}
    bank_by_label = {row["label"]: row for row in dataset["bank_txns"]}

    # The ambiguous pair ("10a"/"10b") shares ONE real bank credit, keyed
    # under "10-shared" rather than either batch's own label -- that's the
    # case's whole point (one bank transaction, two plausible batch
    # matches). Treated as part of "10a"'s own ingestion record (created
    # once, when "10a" is processed); "10b" is still its own real,
    # independent record (own batch/entries/order), it just doesn't create a
    # second copy of a bank credit that only happened once. This keeps "one
    # record per batch label" accurate for all 14 of this dataset's cases,
    # rather than silently dropping the ambiguous pair from the demo.
    shared_bank_row = bank_by_label.pop("10-shared", None)

    accepted = 0
    duplicates = 0
    for batch_row in dataset["batches"]:
        label = batch_row["label"]
        if label == "10a":
            bank_row = shared_bank_row
        elif label == "10b":
            bank_row = None
        else:
            bank_row = bank_by_label.get(label)
        was_new = _ingest_one_batch_idempotent(
            db, batch_row, entries_by_batch.get(label, []), orders_by_ref, bank_row,
        )
        if was_new:
            accepted += 1
        else:
            duplicates += 1

    return IngestionPassResult(
        records_seen=accepted + duplicates, accepted=accepted, duplicates=duplicates,
    )


def run_idempotency_check() -> Dict[str, Any]:
    """Builds one fresh isolated database, runs run_ingestion_pass against it
    TWICE, and returns both passes' results. The second pass's duplicate
    count is only meaningful because it's the same db/table as the first --
    never touches ledgertrail.db either way."""
    dataset = build_holdout_dataset()
    engine = _new_isolated_engine()
    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    try:
        first = run_ingestion_pass(db, dataset)
        second = run_ingestion_pass(db, dataset)
        return {
            "first_ingestion": first,
            "second_ingestion": second,
            "idempotent": second.accepted == 0 and second.duplicates == first.accepted,
        }
    finally:
        db.close()


# --- Feature: approval race demo --------------------------------------------

def build_approval_demo_sandbox() -> Dict[str, Any]:
    """Seeds a sandbox with the full held-out pipeline run (seed -> match ->
    bridge -> classify -> anomaly), then picks one throwaway exception that
    genuinely requires approval and is still open, for the approval-race
    demo to act on. Returns sandbox_id plus that exception's real, freshly
    computed data (never fabricated)."""
    sandbox_id = create_sandbox()
    db = get_sandbox_session(sandbox_id)
    try:
        seed_raw_records(db, sandbox_id)
        run_matching_step(db, sandbox_id)
        run_bridge_step(db)
        run_classification_step(db)

        target = (
            db.query(models.ExceptionRecord)
            .filter(models.ExceptionRecord.status == "open")
            .all()
        )
        candidate = next(
            (e for e in target if CLASSIFICATION_INFO.get(e.classification, {}).get("requires_approval", True)),
            None,
        )
        if candidate is None:
            return {"sandbox_id": sandbox_id, "exception": None}

        entry = _sandbox_entry(sandbox_id)
        label_by_batch_id = {v: k for k, v in entry["batch_id_by_label"].items()}
        return {
            "sandbox_id": sandbox_id,
            "exception": {
                "id": candidate.id,
                "classification": candidate.classification,
                "unexplained_amount": paise_to_rupees(candidate.unexplained_amount),
                "status": candidate.status,
                "batch_label": label_by_batch_id.get(candidate.batch_id),
            },
        }
    finally:
        db.close()
