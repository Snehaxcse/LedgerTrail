"""
Concurrency test for POST /exceptions/{id}/approve's compare-and-set UPDATE
(app/main.py's approve_exception). Fires two genuinely simultaneous approve
calls -- real threads synchronized with a Barrier, not sequential calls --
against the same open exception, seeded in an isolated file-based SQLite DB
shared across both threads. Proves the atomic
"UPDATE ... WHERE status='open'" pattern actually prevents a double-write
under real concurrency, not just under sequential double-clicks: the bug
commit c54aa7e fixed was a prior read-then-write pattern, where two
simultaneous requests could both pass their in-memory "is it open" check
before either one wrote.

This claim previously had no persisted regression test (c54aa7e only
changed app/main.py) -- added here so the README's "approval is atomic,
race-condition tested" claim is actually backed by a permanent test, not
just the original manual verification.

Imports app.main directly (needed to call the real approve_exception()
function, not a reimplementation) and calls it with sessions bound to this
test's own isolated file-based engine -- never app.database.engine/
SessionLocal, so ledgertrail.db is never written to by the approve calls
under test. Importing app.main does run app.database.ensure_schema() once
at module import time as a fixed, pre-existing side effect of importing
that module (unrelated to this test) -- confirmed idempotent/non-mutating
via a byte-for-byte hash check below, the same non-mutation discipline
used throughout this project's held-out/tool-layer/hero-case testing.
"""
import datetime
import hashlib
import threading
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.database import Base

REAL_DB_PATH = Path(__file__).resolve().parent.parent / "ledgertrail.db"


def _hash_real_db():
    if not REAL_DB_PATH.exists():
        return None
    return hashlib.sha256(REAL_DB_PATH.read_bytes()).hexdigest()


def test_importing_app_main_does_not_mutate_real_db():
    before = _hash_real_db()
    import app.main  # noqa: F401  (the import side effect -- ensure_schema() -- is what's under test)

    after = _hash_real_db()
    assert before == after


def _make_open_exception(engine):
    Session = sessionmaker(bind=engine)
    db = Session()
    batch = models.SettlementBatch(
        settlement_date=datetime.date(2026, 1, 1), total_gross=10000, total_refunds=0,
        total_fees=100, total_tax=50, total_net=9850,
    )
    db.add(batch)
    db.flush()
    exc = models.ExceptionRecord(
        batch_id=batch.id, unexplained_amount=500, classification="TIMING_DIFFERENCE",
        suggested_action="test", status="open",
    )
    db.add(exc)
    db.commit()
    exc_id = exc.id
    db.close()
    return exc_id


def test_two_simultaneous_approvals_only_one_wins(tmp_path):
    from fastapi import HTTPException

    from app.main import ApprovalRequest, approve_exception

    db_path = tmp_path / "concurrency_test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    exc_id = _make_open_exception(engine)

    barrier = threading.Barrier(2)
    results = {}
    errors = {}

    def attempt(name, approver, decision, reason=None):
        db = Session()
        try:
            barrier.wait(timeout=5)
            body = ApprovalRequest(approver=approver, decision=decision, reason=reason)
            results[name] = approve_exception(exc_id, body, db)
        except HTTPException as e:
            errors[name] = e
        finally:
            db.close()

    t1 = threading.Thread(target=attempt, args=("t1", "Sneha", "approved"))
    t2 = threading.Thread(
        target=attempt, args=("t2", "Rahul", "rejected"), kwargs={"reason": "concurrency test"}
    )
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert len(results) == 1, f"expected exactly one winner, got {results}"
    assert len(errors) == 1, f"expected exactly one loser, got {errors}"

    (loser_error,) = errors.values()
    assert loser_error.status_code == 409

    verify_db = Session()
    try:
        final = verify_db.query(models.ExceptionRecord).filter(models.ExceptionRecord.id == exc_id).first()
        # Whichever thread's UPDATE the database actually applied first wins --
        # this test doesn't assert WHICH one, only that exactly one did and the
        # final status matches it (no torn/partial write, no double-write).
        assert final.status in ("approved", "rejected")

        logs = verify_db.query(models.ApprovalLog).filter(models.ApprovalLog.exception_id == exc_id).all()
        assert len(logs) == 1, f"expected exactly one ApprovalLog row, got {len(logs)}"
        assert logs[0].decision == final.status
    finally:
        verify_db.close()
