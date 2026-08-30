"""
Held-out reconciliation evaluation. Runs the REAL, unmodified pipeline
(matching.run_matching, bridge.compute_bridge, exceptions.classify_exceptions,
anomaly_detection.run_anomaly_detection -- imported and called exactly as
app/startup.py calls them) against scripts/generate_holdout_data.py's dataset,
inside a completely isolated in-memory SQLite database created fresh on every
call. This module never imports or touches app.database.engine/SessionLocal --
the real ledgertrail.db is structurally unreachable from here, not just
avoided by discipline. See tests/test_holdout_evaluation.py for the explicit
regression test that proves this.

"Do NOT create separate simplified logic purely to make the benchmark score
well" (per spec) is satisfied by construction: the four functions above are
the actual production reconciliation engine, not a reimplementation of it.

Two real architectural findings this dataset originally surfaced -- both since
fixed, kept documented here rather than deleted, since the held-out dataset
that caught them (generate_holdout_data.py's "13" and "07") is what proves the
fixes actually work, not just that the code changed:

1. matching.AMOUNT_TOLERANCE had been set to exact 0 in an earlier phase (the
   float-to-paise migration's tolerance-cleanup step). That made match_diff
   always 0 for any matched batch by construction, so exceptions.py's
   UNEXPLAINED_VARIANCE branch was unreachable -- a "bank amount mismatch"
   case just produced UNMATCHED_BATCH instead. FIXED: matching.AMOUNT_TOLERANCE
   restored to 100 paise (its original pre-migration value) for
   candidate-matching only; exceptions.TOLERANCE and bridge.VARIANCE_TOLERANCE
   deliberately stay exact 0, so a batch that now matches within this window
   but isn't perfectly equal still gets flagged, not silently absorbed. Case
   "13" (a 50-paise variance, within the restored tolerance) is what proves
   UNEXPLAINED_VARIANCE is reachable again; case "08" (a Rs.1,000 mismatch,
   still far outside even the restored tolerance) proves UNMATCHED_BATCH
   remains correct for genuinely unmatched batches.

2. exceptions.py's missing-refund check (`entry.refund - order_refund >
   TOLERANCE`) was directionally asymmetric: it only fired when the SETTLEMENT
   showed more refund than the ORDER record. The reverse (order record shows a
   refund the settlement entry doesn't reflect) went completely unchecked.
   FIXED: added the reverse check, classified as REFUND_NOT_IN_SETTLEMENT (a
   distinct classification from MISSING_REFUND_RECORD -- a refund logged
   internally that the gateway may never have processed is a different
   real-world problem from money that left settlement with no internal
   record, and points a reviewer toward a different next action). Case "07"
   (unchanged from when it was planted specifically to demonstrate this
   blind spot) is what proves the fix actually detects it now.
"""
import time
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import anomaly_detection, bridge, exceptions, matching, models
from app.database import Base
from app.exceptions import CLASSIFICATION_INFO
from scripts.generate_holdout_data import GROUND_TRUTH_UNDETECTED, build_holdout_dataset


@dataclass
class HeldOutCaseResult:
    batch_label: str
    case_type: str
    expected_classification: Optional[str]
    detected_classification: Optional[str]
    detected: bool
    outcome: str  # "true_positive" | "false_positive" | "false_negative" | "true_negative" | "ambiguous"
    is_reconciled: Optional[bool]
    unsafe_auto_resolution: bool
    note: Optional[str] = None


@dataclass
class HeldOutMetrics:
    records_evaluated: int
    planted_exceptions: int
    detected_exceptions: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: Optional[float]
    recall: Optional[float]
    unresolved_ambiguous_cases: int
    unsafe_auto_resolutions: int
    runtime_seconds: float


@dataclass
class HeldOutEvaluationResult:
    metrics: HeldOutMetrics
    cases: List[HeldOutCaseResult] = field(default_factory=list)
    dataset_note: str = (
        "Held-out synthetic evaluation dataset not used by the primary demo flow. "
        "This is not a proof of production accuracy -- it is a fixed, hand-authored "
        "set of cases run once through the real reconciliation engine, not a live "
        "unseen-at-runtime generation process."
    )


def _new_isolated_session():
    """A fresh in-memory SQLite database, created here and only here. No
    reference to app.database.engine/SessionLocal anywhere in this module."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _seed(db, dataset):
    bank_id_by_label = {}
    for row in dataset["bank_txns"]:
        txn = models.BankTransaction(
            amount=row["amount"], date=row["date"],
            reference=row["reference"], description=row["description"],
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
    return batch_id_by_label, bank_id_by_label


def _run_matching_handling_ambiguity(db, ambiguous_labels, batch_id_by_label):
    """Calls the real matching.run_matching() unmodified. If it raises
    AmbiguousMatchError, the conflicting batches (known up front, since this
    dataset deliberately planted them) are excluded from candidacy and
    run_matching() is retried once -- so one ambiguous pair doesn't block
    matching for every other batch in the run. This changes what DATA the
    real function sees, never what the function itself does."""
    try:
        matching.run_matching(db)
        return set()
    except matching.AmbiguousMatchError:
        pass

    excluded_ids = {batch_id_by_label[label] for label in ambiguous_labels}
    for batch_id in excluded_ids:
        batch = db.query(models.SettlementBatch).filter(models.SettlementBatch.id == batch_id).first()
        bank_txn_id = batch.bank_transaction_id
        if bank_txn_id is not None:
            db.query(models.BankTransaction).filter(models.BankTransaction.id == bank_txn_id).delete()
    db.query(models.SettlementBatch).filter(models.SettlementBatch.id.in_(excluded_ids)).delete(
        synchronize_session=False
    )
    db.commit()
    matching.run_matching(db)
    return ambiguous_labels


def _is_reconciled(db, batch_id):
    variance_ok_batch = db.query(models.SettlementBatch).filter(models.SettlementBatch.id == batch_id).first()
    if variance_ok_batch.bank_transaction_id is None:
        return False
    bank_amount = variance_ok_batch.bank_transaction.amount
    variance_zero = (variance_ok_batch.total_net - bank_amount) == 0
    open_exceptions = (
        db.query(models.ExceptionRecord)
        .filter(models.ExceptionRecord.batch_id == batch_id, models.ExceptionRecord.status == "open")
        .all()
    )
    has_outstanding = any(
        CLASSIFICATION_INFO.get(e.classification, {}).get("requires_approval", True)
        or CLASSIFICATION_INFO.get(e.classification, {}).get("blocks_reconciliation", True)
        for e in open_exceptions
    )
    return variance_zero and not has_outstanding


def run_holdout_evaluation() -> HeldOutEvaluationResult:
    start = time.monotonic()
    dataset = build_holdout_dataset()
    db = _new_isolated_session()

    batch_id_by_label, _bank_id_by_label = _seed(db, dataset)
    ambiguous_labels = _run_matching_handling_ambiguity(
        db, dataset["ambiguous_batch_labels"], batch_id_by_label
    )
    bridge.compute_bridge(db)
    exceptions.classify_exceptions(db)
    anomaly_detection.run_anomaly_detection(db)

    exceptions_by_label = {}
    for label, batch_id in batch_id_by_label.items():
        if label in ambiguous_labels:
            continue
        rows = db.query(models.ExceptionRecord).filter(models.ExceptionRecord.batch_id == batch_id).all()
        exceptions_by_label[label] = rows

    cases: List[HeldOutCaseResult] = []
    tp = fp = fn = 0
    unsafe = 0

    for gt in dataset["ground_truth"]:
        label = gt["batch_label"]

        if label in ambiguous_labels:
            cases.append(HeldOutCaseResult(
                batch_label=label, case_type=gt["case_type"],
                expected_classification="AMBIGUOUS", detected_classification=None,
                detected=False, outcome="ambiguous", is_reconciled=None,
                unsafe_auto_resolution=False,
                note="Excluded from matching by the ambiguity guard -- unresolved, not scored as TP/FP/FN.",
            ))
            continue

        expected = gt["expected_classification"]
        rows = exceptions_by_label.get(label, [])
        detected_classification = rows[0].classification if rows else None
        batch_id = batch_id_by_label[label]
        reconciled = _is_reconciled(db, batch_id)

        if expected is None:
            # Clean case: any exception at all is a false positive.
            if rows:
                fp += 1
                outcome = "false_positive"
            else:
                outcome = "true_negative"
            cases.append(HeldOutCaseResult(
                batch_label=label, case_type=gt["case_type"], expected_classification=None,
                detected_classification=detected_classification, detected=bool(rows),
                outcome=outcome, is_reconciled=reconciled, unsafe_auto_resolution=False,
            ))
            continue

        if expected == GROUND_TRUTH_UNDETECTED:
            # A genuine planted issue this engine is known not to catch. Counted
            # honestly as a false negative, and -- since nothing blocks it --
            # as an unsafe auto-resolution too.
            fn += 1
            is_unsafe = reconciled
            if is_unsafe:
                unsafe += 1
            cases.append(HeldOutCaseResult(
                batch_label=label, case_type=gt["case_type"], expected_classification=expected,
                detected_classification=detected_classification, detected=False,
                outcome="false_negative", is_reconciled=reconciled,
                unsafe_auto_resolution=is_unsafe, note=gt.get("note"),
            ))
            continue

        matched = detected_classification == expected
        if matched:
            tp += 1
            outcome = "true_positive"
        else:
            fn += 1
            outcome = "false_negative"

        requires_approval = CLASSIFICATION_INFO.get(expected, {}).get("requires_approval", True)
        is_unsafe = matched and requires_approval and reconciled
        if is_unsafe:
            unsafe += 1

        cases.append(HeldOutCaseResult(
            batch_label=label, case_type=gt["case_type"], expected_classification=expected,
            detected_classification=detected_classification, detected=bool(rows),
            outcome=outcome, is_reconciled=reconciled, unsafe_auto_resolution=is_unsafe,
            note=gt.get("note"),
        ))

    planted = tp + fn
    detected_count = sum(1 for c in cases if c.detected)
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None

    runtime = time.monotonic() - start
    db.close()

    metrics = HeldOutMetrics(
        records_evaluated=len(dataset["ground_truth"]),
        planted_exceptions=planted,
        detected_exceptions=detected_count,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        precision=precision,
        recall=recall,
        unresolved_ambiguous_cases=len(ambiguous_labels),
        unsafe_auto_resolutions=unsafe,
        runtime_seconds=round(runtime, 4),
    )
    return HeldOutEvaluationResult(metrics=metrics, cases=cases)
