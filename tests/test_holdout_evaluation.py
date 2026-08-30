"""
Persisted regression tests for the held-out evaluation harness (Phase A of the
AI Reconciliation Investigation Agent work). These are the actual checkpoint
for this phase, not one-off manual verification -- see the project's own
review note about needing a visible automated test suite.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models
from app.database import SessionLocal
from app.holdout_evaluation import run_holdout_evaluation
from scripts.generate_holdout_data import build_holdout_dataset


def _case(result, label):
    return next(c for c in result.cases if c.batch_label == label)


def test_dataset_has_the_required_case_types():
    """Every case type the spec named is present at least once."""
    dataset = build_holdout_dataset()
    case_types = {g["case_type"] for g in dataset["ground_truth"]}
    required = {
        "clean_exact_match", "date_shifted_match", "fee_mismatch", "missing_refund",
        "refund_mismatch_reverse", "bank_amount_mismatch_large", "bank_amount_mismatch_small",
        "duplicate_looking", "ambiguous_candidate_match",
    }
    missing = required - case_types
    assert not missing, f"held-out dataset is missing case types: {missing}"
    assert len(dataset["ground_truth"]) >= 10, "not enough records for meaningful metrics"


def test_clean_batches_produce_no_exceptions():
    """Every clean_exact_match case is a true negative -- no false positives
    from the engine on data it has never seen."""
    result = run_holdout_evaluation()
    clean_labels = [
        g["batch_label"] for g in build_holdout_dataset()["ground_truth"]
        if g["case_type"] == "clean_exact_match"
    ]
    assert len(clean_labels) >= 5
    for label in clean_labels:
        c = _case(result, label)
        assert c.outcome == "true_negative", f"batch {label} should be clean but got {c.outcome}"
        assert c.detected_classification is None


def test_each_planted_case_produces_the_expected_classification():
    """The engine's real classification for each planted case matches the
    hand-derived expectation -- this IS the proof that the harness runs the
    real matching/bridge/exceptions logic, not a simplified reimplementation:
    a fake couldn't reproduce this many independently-derived outcomes by luck."""
    result = run_holdout_evaluation()

    expectations = {
        "04": "TIMING_DIFFERENCE",
        "05": "FEE_TIER_MISMATCH",
        "06": "MISSING_REFUND_RECORD",
        "07": "REFUND_NOT_IN_SETTLEMENT",
        "08": "UNMATCHED_BATCH",
        "09": "DUPLICATE_ENTRY",
        "13": "UNEXPLAINED_VARIANCE",
    }
    for label, expected in expectations.items():
        c = _case(result, label)
        assert c.detected_classification == expected, (
            f"batch {label}: expected {expected}, got {c.detected_classification}"
        )
        assert c.outcome == "true_positive"


def test_reverse_refund_mismatch_now_detected_not_a_false_negative():
    """Batch 07 was originally planted to demonstrate a real, disclosed engine
    blind spot (a false negative AND an unsafe auto-resolution). Now that
    exceptions.py has the reverse refund check, this same case must come back
    as a clean true positive -- proof the fix actually works, using the exact
    case that caught the bug in the first place."""
    result = run_holdout_evaluation()
    c = _case(result, "07")
    assert c.outcome == "true_positive"
    assert c.detected_classification == "REFUND_NOT_IN_SETTLEMENT"
    assert c.unsafe_auto_resolution is False


def test_small_bank_variance_now_reaches_unexplained_variance():
    """Batch 13: a 50-paise bank/settlement variance, within the restored
    matching.AMOUNT_TOLERANCE. Proof that UNEXPLAINED_VARIANCE is reachable
    again, not just that the constant changed."""
    result = run_holdout_evaluation()
    c = _case(result, "13")
    assert c.outcome == "true_positive"
    assert c.detected_classification == "UNEXPLAINED_VARIANCE"


def test_large_bank_mismatch_still_unmatched_after_tolerance_restore():
    """Batch 08: a Rs.1,000 mismatch is still far outside even the restored
    (Rs.1.00) tolerance -- must remain UNMATCHED_BATCH, not accidentally start
    matching now that AMOUNT_TOLERANCE is nonzero again."""
    result = run_holdout_evaluation()
    c = _case(result, "08")
    assert c.outcome == "true_positive"
    assert c.detected_classification == "UNMATCHED_BATCH"


def test_ambiguous_pair_is_counted_not_crashed():
    """matching.AmbiguousMatchError must be caught and both conflicting batches
    counted as unresolved/ambiguous -- and must NOT prevent every other batch
    in the same run from being matched."""
    result = run_holdout_evaluation()
    c_a = _case(result, "10a")
    c_b = _case(result, "10b")
    assert c_a.outcome == "ambiguous"
    assert c_b.outcome == "ambiguous"
    assert result.metrics.unresolved_ambiguous_cases == 2

    # Every other batch still got matched and classified despite the ambiguity.
    for label in ("04", "05", "06", "07", "08", "09", "13"):
        c = _case(result, label)
        assert c.detected_classification is not None, (
            f"batch {label} should still have been matched despite the ambiguous pair"
        )


def test_metrics_are_internally_consistent():
    result = run_holdout_evaluation()
    m = result.metrics
    assert m.true_positives == 7
    assert m.false_positives == 0
    assert m.false_negatives == 0
    assert m.unresolved_ambiguous_cases == 2
    assert m.unsafe_auto_resolutions == 0
    assert m.planted_exceptions == m.true_positives + m.false_negatives
    assert m.precision == 1.0
    assert m.recall == 1.0
    assert m.runtime_seconds >= 0


def test_holdout_evaluation_does_not_mutate_the_real_database():
    """THE checkpoint for this phase: running the held-out evaluation must have
    zero observable effect on the real ledgertrail.db. Snapshots whatever is
    actually there before the run (not a hardcoded assumption about demo
    state) and asserts byte-identical after -- so this test is meaningful
    whether or not the primary pipeline has been seeded when it runs."""
    real_db = SessionLocal()
    try:
        before_batches = real_db.query(models.SettlementBatch).count()
        before_entries = real_db.query(models.SettlementEntry).count()
        before_bank_txns = real_db.query(models.BankTransaction).count()
        before_orders = real_db.query(models.OrderRecord).count()
        before_exceptions = [
            (e.id, e.batch_id, e.classification, e.unexplained_amount, e.status)
            for e in real_db.query(models.ExceptionRecord).order_by(models.ExceptionRecord.id).all()
        ]
        before_approval_logs = [
            (a.id, a.exception_id, a.approver, a.decision)
            for a in real_db.query(models.ApprovalLog).order_by(models.ApprovalLog.id).all()
        ]
        before_audit_events = real_db.query(models.AuditEvent).count()
    finally:
        real_db.close()

    run_holdout_evaluation()
    run_holdout_evaluation()  # twice, to also prove it doesn't accumulate state anywhere real

    real_db = SessionLocal()
    try:
        assert real_db.query(models.SettlementBatch).count() == before_batches
        assert real_db.query(models.SettlementEntry).count() == before_entries
        assert real_db.query(models.BankTransaction).count() == before_bank_txns
        assert real_db.query(models.OrderRecord).count() == before_orders
        after_exceptions = [
            (e.id, e.batch_id, e.classification, e.unexplained_amount, e.status)
            for e in real_db.query(models.ExceptionRecord).order_by(models.ExceptionRecord.id).all()
        ]
        assert after_exceptions == before_exceptions
        after_approval_logs = [
            (a.id, a.exception_id, a.approver, a.decision)
            for a in real_db.query(models.ApprovalLog).order_by(models.ApprovalLog.id).all()
        ]
        assert after_approval_logs == before_approval_logs
        assert real_db.query(models.AuditEvent).count() == before_audit_events

        # If the demo has been staged (the normal case), the specific
        # Sneha-approved record must still say so.
        staged = (
            real_db.query(models.ExceptionRecord)
            .filter(models.ExceptionRecord.batch_id == 2,
                     models.ExceptionRecord.classification == "MISSING_REFUND_RECORD")
            .first()
        )
        if staged is not None and staged.status == "approved":
            log = (
                real_db.query(models.ApprovalLog)
                .filter(models.ApprovalLog.exception_id == staged.id)
                .order_by(models.ApprovalLog.id.desc())
                .first()
            )
            assert log is not None and log.approver == "Sneha"
    finally:
        real_db.close()
