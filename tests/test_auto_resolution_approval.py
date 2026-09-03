"""
Phase 3 regression tests for the approve endpoint's resolution_method plumbing
(app/main.py's _approve_exception_core / ApprovalRequest.resolution_method).

Core guarantee under test: resolution_method="policy_confirmed" is NEVER
trusted at face value. The endpoint re-runs app.policy's deterministic
eligibility check itself against the current row before honoring it -- a
client claiming an ineligible exception is "policy_confirmed" must get a 400,
not a silent downgrade to a manual approval.

All against an isolated in-memory SQLite DB (never app.database.engine/
SessionLocal, never ledgertrail.db), same discipline as
tests/test_approve_concurrency.py and tests/test_policy.py.
"""
import datetime
import json

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.auth import AuthenticatedUser
from app.database import Base
from app.main import ApprovalRequest, approve_exception

# approve_exception derives actor from current_user (an authenticated session),
# never from body.approver (see app/auth.py) -- this fake stands in for a
# logged-in approver across every test below.
SNEHA = AuthenticatedUser(id=1, username="sneha", role="approver", display_name="Sneha", job_title="Finance Analyst")


def _isolated_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


CLEAN_INVESTIGATION = {
    "verified_facts": ["The settlement entry's refund matches the order record."],
    "unverified_claims": [],
    "contradictions": [],
}


def _make_exception(db, *, bank_amount=9850, total_net=9850, severity="medium", investigation_result=None):
    batch = models.SettlementBatch(
        settlement_date=datetime.date(2026, 1, 1),
        total_gross=10000, total_refunds=0, total_fees=100, total_tax=50,
        total_net=total_net,
    )
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
    return exc


def test_policy_confirmed_succeeds_for_a_genuinely_eligible_exception():
    db = _isolated_db()
    try:
        exc = _make_exception(db, severity="low", investigation_result=CLEAN_INVESTIGATION)
        body = ApprovalRequest(decision="approved", resolution_method="policy_confirmed")
        response = approve_exception(exc.id, body, db, current_user=SNEHA)

        assert response.status == "approved"
        assert response.resolution_method == "policy_confirmed"

        log = db.query(models.ApprovalLog).filter(models.ApprovalLog.exception_id == exc.id).first()
        assert "policy-confirmed" in log.resulting_action

        event = (
            db.query(models.AuditEvent)
            .filter(models.AuditEvent.action == "exception_reviewed")
            .order_by(models.AuditEvent.id.desc())
            .first()
        )
        after = json.loads(event.after_state)
        assert after["resolution_method"] == "policy_confirmed"
    finally:
        db.close()


def test_policy_confirmed_rejected_when_severity_high():
    db = _isolated_db()
    try:
        exc = _make_exception(db, severity="high", investigation_result=CLEAN_INVESTIGATION)
        body = ApprovalRequest(decision="approved", resolution_method="policy_confirmed")
        try:
            approve_exception(exc.id, body, db, current_user=SNEHA)
            assert False, "expected HTTPException"
        except HTTPException as e:
            assert e.status_code == 400
            assert "not eligible" in e.detail.lower()

        # The row must be untouched -- a rejected eligibility claim is not a
        # partial write. Still open, no ApprovalLog, no exception_reviewed AuditEvent.
        refreshed = db.query(models.ExceptionRecord).filter(models.ExceptionRecord.id == exc.id).first()
        assert refreshed.status == "open"
        assert db.query(models.ApprovalLog).filter(models.ApprovalLog.exception_id == exc.id).count() == 0
    finally:
        db.close()


def test_policy_confirmed_rejected_when_variance_nonzero():
    db = _isolated_db()
    try:
        exc = _make_exception(
            db, bank_amount=9800, total_net=9850, investigation_result=CLEAN_INVESTIGATION,
        )
        body = ApprovalRequest(decision="approved", resolution_method="policy_confirmed")
        try:
            approve_exception(exc.id, body, db, current_user=SNEHA)
            assert False, "expected HTTPException"
        except HTTPException as e:
            assert e.status_code == 400
            assert "variance" in e.detail.lower()
    finally:
        db.close()


def test_policy_confirmed_rejected_when_no_investigation_yet():
    db = _isolated_db()
    try:
        exc = _make_exception(db, investigation_result=None)
        body = ApprovalRequest(decision="approved", resolution_method="policy_confirmed")
        try:
            approve_exception(exc.id, body, db, current_user=SNEHA)
            assert False, "expected HTTPException"
        except HTTPException as e:
            assert e.status_code == 400
    finally:
        db.close()


def test_policy_confirmed_rejected_when_paired_with_reject_decision():
    """Auto-resolve is a confirmation of an approval, not a rejection --
    resolution_method='policy_confirmed' with decision='rejected' must never
    be silently accepted as some third kind of outcome."""
    db = _isolated_db()
    try:
        exc = _make_exception(db, severity="low", investigation_result=CLEAN_INVESTIGATION)
        body = ApprovalRequest(
            decision="rejected", reason="test", resolution_method="policy_confirmed",
        )
        try:
            approve_exception(exc.id, body, db, current_user=SNEHA)
            assert False, "expected HTTPException"
        except HTTPException as e:
            assert e.status_code == 400
            assert "only valid with decision='approved'" in e.detail
    finally:
        db.close()


def test_manual_approval_unaffected_defaults_to_manual_resolution_method():
    """Regression: a plain approve/reject call (no resolution_method in the
    request body at all, exactly like every pre-existing caller) must behave
    identically to before this feature existed."""
    db = _isolated_db()
    try:
        exc = _make_exception(db, severity="high", investigation_result=None)  # would be policy-ineligible
        body = ApprovalRequest(decision="approved")
        response = approve_exception(exc.id, body, db, current_user=SNEHA)

        assert response.status == "approved"
        assert response.resolution_method == "manual"

        log = db.query(models.ApprovalLog).filter(models.ApprovalLog.exception_id == exc.id).first()
        assert log.resulting_action == "status set to 'approved'"
    finally:
        db.close()
