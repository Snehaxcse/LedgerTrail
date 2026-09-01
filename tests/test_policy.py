"""
Phase 1 regression tests for the deterministic auto-resolution policy check
(app/policy.py). Two groups:

  1. Pure eligibility-branch tests against an isolated in-memory SQLite DB
     (never app.database.engine/SessionLocal, never ledgertrail.db) --
     covers every individual blocking condition in
     check_auto_resolution_eligibility.
  2. A live, read-only cross-check against the REAL ledgertrail.db proving
     bridge.compute_bridge()'s variance agrees with app.main._batch_summary's
     independently-computed variance for every real batch -- see policy.py's
     module docstring for why this duplication exists and why it's guarded
     here rather than silently trusted.
"""
import datetime
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models, policy
from app.database import Base


def _isolated_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _make_batch_and_exception(
    db,
    *,
    bank_amount=9850,
    total_net=9850,
    severity="medium",
    investigation_result=None,
    matched=True,
):
    batch = models.SettlementBatch(
        settlement_date=datetime.date(2026, 1, 1),
        total_gross=10000, total_refunds=0, total_fees=100, total_tax=50,
        total_net=total_net,
    )
    if matched:
        txn = models.BankTransaction(
            amount=bank_amount, date=datetime.date(2026, 1, 2), reference="REF-1", description=None,
        )
        db.add(txn)
        db.flush()
        batch.bank_transaction_id = txn.id
    db.add(batch)
    db.flush()

    exc = models.ExceptionRecord(
        batch_id=batch.id, unexplained_amount=500, classification="TIMING_DIFFERENCE",
        suggested_action="test", status="open", severity=severity,
        investigation_result=json.dumps(investigation_result) if investigation_result is not None else None,
    )
    db.add(exc)
    db.commit()
    return batch, exc


CLEAN_INVESTIGATION = {
    "verified_facts": ["The settlement entry's refund of ₹57.87 matches the order record."],
    "unverified_claims": [],
    "contradictions": [],
}


def test_eligible_when_variance_zero_clean_investigation_non_high_severity():
    db = _isolated_db()
    try:
        _, exc = _make_batch_and_exception(
            db, bank_amount=9850, total_net=9850, severity="low", investigation_result=CLEAN_INVESTIGATION,
        )
        variance = policy.compute_variance_by_batch(db)[exc.batch_id]
        result = policy.check_auto_resolution_eligibility(exc, variance)
        assert result.eligible is True
    finally:
        db.close()


def test_ineligible_when_no_investigation_result_yet():
    db = _isolated_db()
    try:
        _, exc = _make_batch_and_exception(db, investigation_result=None)
        variance = policy.compute_variance_by_batch(db)[exc.batch_id]
        result = policy.check_auto_resolution_eligibility(exc, variance)
        assert result.eligible is False
        assert "no ai investigation" in result.reason.lower()
    finally:
        db.close()


def test_ineligible_when_severity_high_even_with_clean_investigation():
    db = _isolated_db()
    try:
        _, exc = _make_batch_and_exception(
            db, severity="high", investigation_result=CLEAN_INVESTIGATION,
        )
        variance = policy.compute_variance_by_batch(db)[exc.batch_id]
        result = policy.check_auto_resolution_eligibility(exc, variance)
        assert result.eligible is False
        assert "severity is high" in result.reason.lower()
    finally:
        db.close()


def test_ineligible_when_unverified_claims_present():
    db = _isolated_db()
    try:
        investigation = dict(CLEAN_INVESTIGATION, unverified_claims=["AI's own interpretation, unconfirmed"])
        _, exc = _make_batch_and_exception(db, investigation_result=investigation)
        variance = policy.compute_variance_by_batch(db)[exc.batch_id]
        result = policy.check_auto_resolution_eligibility(exc, variance)
        assert result.eligible is False
        assert "unverified claims" in result.reason.lower()
    finally:
        db.close()


def test_ineligible_when_contradictions_present():
    db = _isolated_db()
    try:
        investigation = dict(CLEAN_INVESTIGATION, contradictions=["claim [REJECTED BY VERIFIER: ...]"])
        _, exc = _make_batch_and_exception(db, investigation_result=investigation)
        variance = policy.compute_variance_by_batch(db)[exc.batch_id]
        result = policy.check_auto_resolution_eligibility(exc, variance)
        assert result.eligible is False
        assert "contradictions" in result.reason.lower()
    finally:
        db.close()


def test_ineligible_when_no_verified_facts():
    db = _isolated_db()
    try:
        investigation = dict(CLEAN_INVESTIGATION, verified_facts=[])
        _, exc = _make_batch_and_exception(db, investigation_result=investigation)
        variance = policy.compute_variance_by_batch(db)[exc.batch_id]
        result = policy.check_auto_resolution_eligibility(exc, variance)
        assert result.eligible is False
        assert "no verified facts" in result.reason.lower()
    finally:
        db.close()


def test_ineligible_when_bank_variance_nonzero():
    db = _isolated_db()
    try:
        _, exc = _make_batch_and_exception(
            db, bank_amount=9800, total_net=9850, investigation_result=CLEAN_INVESTIGATION,
        )
        variance = policy.compute_variance_by_batch(db)[exc.batch_id]
        assert variance == 50
        result = policy.check_auto_resolution_eligibility(exc, variance)
        assert result.eligible is False
        assert "variance is not zero" in result.reason.lower()
    finally:
        db.close()


def test_ineligible_when_batch_unmatched_to_bank():
    db = _isolated_db()
    try:
        _, exc = _make_batch_and_exception(db, matched=False, investigation_result=CLEAN_INVESTIGATION)
        variance = policy.compute_variance_by_batch(db)[exc.batch_id]
        assert variance is None
        result = policy.check_auto_resolution_eligibility(exc, variance)
        assert result.eligible is False
        assert "not matched to a bank transaction" in result.reason.lower()
    finally:
        db.close()


def test_ineligible_when_investigation_result_unparseable():
    db = _isolated_db()
    try:
        _, exc = _make_batch_and_exception(db, investigation_result=CLEAN_INVESTIGATION)
        exc.investigation_result = "not json"
        db.commit()
        variance = policy.compute_variance_by_batch(db)[exc.batch_id]
        result = policy.check_auto_resolution_eligibility(exc, variance)
        assert result.eligible is False
        assert "could not be parsed" in result.reason.lower()
    finally:
        db.close()


def test_check_auto_resolution_eligibility_for_batch_wrapper_matches_manual_lookup():
    db = _isolated_db()
    try:
        _, exc = _make_batch_and_exception(db, investigation_result=CLEAN_INVESTIGATION)
        via_wrapper = policy.check_auto_resolution_eligibility_for_batch(db, exc)
        variance = policy.compute_variance_by_batch(db)[exc.batch_id]
        via_manual = policy.check_auto_resolution_eligibility(exc, variance)
        assert via_wrapper == via_manual
    finally:
        db.close()


# ---------- live, read-only cross-check against the real DB ----------

def test_bridge_variance_agrees_with_dashboard_variance_for_every_real_batch():
    """Guards the exact duplication policy.py's docstring calls out: bridge.py's
    compute_bridge() and app.main's _batch_summary() are two independent
    implementations of "total_net - matched bank amount". They must agree for
    every real batch, or the policy layer and the dashboard would silently
    disagree about what "reconciled" means. Read-only: no db.add()/commit()
    anywhere in this test."""
    from app import bridge
    from app.database import SessionLocal
    from app.main import _batch_summary

    db = SessionLocal()
    try:
        bridge_variance_by_batch = {r.batch_id: r.variance for r in bridge.compute_bridge(db)}
        batches = db.query(models.SettlementBatch).all()
        assert batches, "expected at least one real batch to cross-check against"
        for batch in batches:
            summary = _batch_summary(db, batch)
            dashboard_variance_paise = (
                None if summary.variance is None else round(summary.variance * 100)
            )
            assert bridge_variance_by_batch[batch.id] == dashboard_variance_paise, (
                f"batch {batch.id}: bridge.compute_bridge variance "
                f"{bridge_variance_by_batch[batch.id]} paise != dashboard variance "
                f"{dashboard_variance_paise} paise"
            )
    finally:
        db.close()
