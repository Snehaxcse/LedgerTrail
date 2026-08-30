"""
Focused unit tests for the two engine fixes made after Phase A's held-out
evaluation surfaced them:

1. matching.AMOUNT_TOLERANCE restored to 100 paise (candidate-matching only;
   exceptions.TOLERANCE and bridge.VARIANCE_TOLERANCE stay exact 0).
2. exceptions.py's missing-refund check gained a reverse direction
   (REFUND_NOT_IN_SETTLEMENT), fixing a real directional blind spot.

These seed minimal, hand-built data directly via the scratch_db fixture
(conftest.py) rather than going through the held-out dataset generator --
tight, surgical coverage of the exact boundary conditions, complementary to
tests/test_holdout_evaluation.py's broader end-to-end coverage of the same
fixes.
"""
import datetime

from app import bridge, exceptions, matching, models


def _seed_matched_batch(db, batch_id_label, gross, fee, tax, refund, bank_amount,
                          settlement_date=None, order_refund=None, order_fee=None):
    """One batch, one entry, one matching order record, one bank transaction --
    the minimum shape needed to exercise matching + classify_exceptions."""
    settlement_date = settlement_date or datetime.date(2027, 1, 1)
    net = gross - refund - fee - tax

    txn = models.BankTransaction(
        amount=bank_amount, date=settlement_date,
        reference=f"UTR{batch_id_label}", description="NEFT CR: HDFC BANK TEST",
    )
    db.add(txn)
    db.flush()

    batch = models.SettlementBatch(
        settlement_date=settlement_date, total_gross=gross, total_refunds=refund,
        total_fees=fee, total_tax=tax, total_net=net,
    )
    db.add(batch)
    db.flush()

    order_ref = f"TEST-{batch_id_label}"
    db.add(models.SettlementEntry(
        batch_id=batch.id, order_ref=order_ref, gross_amount=gross,
        fee=fee, tax=tax, refund=refund, net_amount=net,
    ))
    db.add(models.OrderRecord(
        order_ref=order_ref, amount=gross, status="completed",
        refund_amount=order_refund if order_refund is not None else refund,
        fee_amount=order_fee if order_fee is not None else fee,
    ))
    db.commit()
    return batch.id


def _run_pipeline(db):
    matching.run_matching(db)
    bridge.compute_bridge(db)
    exceptions.classify_exceptions(db)


# --- Fix 1: AMOUNT_TOLERANCE boundary ------------------------------------

def test_exact_amount_still_matches(scratch_db):
    batch_id = _seed_matched_batch(
        scratch_db, "exact", gross=1_000_000, fee=25_000, tax=4_500, refund=0,
        bank_amount=1_000_000 - 25_000 - 4_500,
    )
    _run_pipeline(scratch_db)
    batch = scratch_db.query(models.SettlementBatch).filter(models.SettlementBatch.id == batch_id).first()
    assert batch.bank_transaction_id is not None


def test_variance_within_restored_tolerance_still_matches_and_is_flagged(scratch_db):
    """100 paise variance -- exactly at matching.AMOUNT_TOLERANCE's boundary --
    must still match, and (since exceptions.TOLERANCE is exact 0) must still be
    flagged as UNEXPLAINED_VARIANCE, not silently absorbed by the match."""
    net = 1_000_000 - 25_000 - 4_500
    batch_id = _seed_matched_batch(
        scratch_db, "boundary", gross=1_000_000, fee=25_000, tax=4_500, refund=0,
        bank_amount=net - matching.AMOUNT_TOLERANCE,
    )
    _run_pipeline(scratch_db)
    batch = scratch_db.query(models.SettlementBatch).filter(models.SettlementBatch.id == batch_id).first()
    assert batch.bank_transaction_id is not None, "should still match at the tolerance boundary"

    exc = scratch_db.query(models.ExceptionRecord).filter(models.ExceptionRecord.batch_id == batch_id).first()
    assert exc is not None
    assert exc.classification == "UNEXPLAINED_VARIANCE"
    assert exc.unexplained_amount == matching.AMOUNT_TOLERANCE


def test_variance_just_outside_tolerance_does_not_match(scratch_db):
    net = 1_000_000 - 25_000 - 4_500
    batch_id = _seed_matched_batch(
        scratch_db, "outside", gross=1_000_000, fee=25_000, tax=4_500, refund=0,
        bank_amount=net - matching.AMOUNT_TOLERANCE - 1,
    )
    _run_pipeline(scratch_db)
    batch = scratch_db.query(models.SettlementBatch).filter(models.SettlementBatch.id == batch_id).first()
    assert batch.bank_transaction_id is None, "should NOT match just past the tolerance boundary"

    exc = scratch_db.query(models.ExceptionRecord).filter(models.ExceptionRecord.batch_id == batch_id).first()
    assert exc is not None
    assert exc.classification == "UNMATCHED_BATCH"


# --- Fix 2: reverse-direction refund check --------------------------------

def test_forward_direction_still_detected_as_missing_refund_record(scratch_db):
    """Regression guard: the ORIGINAL direction (settlement shows a refund the
    order doesn't) must still classify as MISSING_REFUND_RECORD, unaffected by
    adding the reverse check."""
    net = 1_000_000 - 200_000 - 25_000 - 4_500
    batch_id = _seed_matched_batch(
        scratch_db, "fwd", gross=1_000_000, fee=25_000, tax=4_500, refund=200_000,
        bank_amount=net, order_refund=0,
    )
    _run_pipeline(scratch_db)
    exc = scratch_db.query(models.ExceptionRecord).filter(models.ExceptionRecord.batch_id == batch_id).first()
    assert exc.classification == "MISSING_REFUND_RECORD"
    assert exc.unexplained_amount == 200_000


def test_reverse_direction_now_detected_as_refund_not_in_settlement(scratch_db):
    """THE fix: order record shows a refund the settlement entry doesn't
    reflect -- previously undetected entirely, must now classify as
    REFUND_NOT_IN_SETTLEMENT with the correct delta."""
    net = 1_000_000 - 25_000 - 4_500
    batch_id = _seed_matched_batch(
        scratch_db, "rev", gross=1_000_000, fee=25_000, tax=4_500, refund=0,
        bank_amount=net, order_refund=150_000,
    )
    _run_pipeline(scratch_db)
    exc = scratch_db.query(models.ExceptionRecord).filter(models.ExceptionRecord.batch_id == batch_id).first()
    assert exc is not None, "reverse-direction refund mismatch must now be detected"
    assert exc.classification == "REFUND_NOT_IN_SETTLEMENT"
    assert exc.unexplained_amount == 150_000
    assert exceptions.CLASSIFICATION_INFO["REFUND_NOT_IN_SETTLEMENT"]["requires_approval"] is True


def test_equal_refunds_both_directions_raise_no_refund_exception(scratch_db):
    """Sanity guard: when settlement and order agree exactly, neither refund
    check should fire."""
    net = 1_000_000 - 200_000 - 25_000 - 4_500
    batch_id = _seed_matched_batch(
        scratch_db, "equal", gross=1_000_000, fee=25_000, tax=4_500, refund=200_000,
        bank_amount=net, order_refund=200_000,
    )
    _run_pipeline(scratch_db)
    exc = scratch_db.query(models.ExceptionRecord).filter(models.ExceptionRecord.batch_id == batch_id).first()
    assert exc is None
