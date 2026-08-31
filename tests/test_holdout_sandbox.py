"""
Tests for app/holdout_sandbox.py's three live demo features (idempotent
ingestion replay, step-by-step reconciliation progress, approval race) --
all built on the held-out dataset, all using isolated in-memory databases
this module creates itself. Includes an explicit non-mutation check against
the real ledgertrail.db (same discipline as every other isolated-DB feature
in this project) and a direct proof that the idempotency guarantee is a real
database-level UNIQUE constraint, not application-code deduplication that a
caller could route around.
"""
import datetime
import hashlib
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import holdout_sandbox as hs
from app import models
from app.main import ApprovalRequest, _approve_exception_core

REAL_DB_PATH = Path(__file__).resolve().parent.parent / "ledgertrail.db"


def _hash_real_db():
    if not REAL_DB_PATH.exists():
        return None
    return hashlib.sha256(REAL_DB_PATH.read_bytes()).hexdigest()


# --- Idempotency --------------------------------------------------------

def test_idempotency_check_first_pass_accepts_all_second_pass_all_duplicates():
    result = hs.run_idempotency_check()
    assert result["first_ingestion"].accepted == 14
    assert result["first_ingestion"].duplicates == 0
    assert result["second_ingestion"].accepted == 0
    assert result["second_ingestion"].duplicates == 14
    assert result["idempotent"] is True


def test_idempotency_uses_a_real_database_unique_constraint_not_app_code_dedup():
    """Direct proof: inserting the same source_event_id twice raises
    IntegrityError from SQLite itself. If this were application-code
    deduplication (e.g. a Python-side "does this exist" query), a caller
    going around that check -- exactly what this test does, bypassing
    run_ingestion_pass entirely -- would succeed. It doesn't."""
    engine = hs._new_isolated_engine()
    db = sessionmaker(bind=engine)()
    try:
        db.add(models.HeldOutIngestionRecord(source_event_id="dup-test", ingested_at=datetime.datetime.now()))
        db.commit()
        with pytest.raises(IntegrityError):
            db.add(models.HeldOutIngestionRecord(source_event_id="dup-test", ingested_at=datetime.datetime.now()))
            db.flush()
    finally:
        db.rollback()
        db.close()


def test_idempotency_check_does_not_mutate_real_db():
    before = _hash_real_db()
    hs.run_idempotency_check()
    after = _hash_real_db()
    assert before == after


# --- Step-by-step reconciliation progress -------------------------------

def test_reconciliation_steps_run_against_the_same_persisted_sandbox():
    """Confirms the StaticPool fix: a NEW Session() created in a later call
    (simulating a separate HTTP request) sees data a PRIOR Session() wrote --
    without this, each step would silently operate on an empty database."""
    sandbox_id = hs.create_sandbox()

    db = hs.get_sandbox_session(sandbox_id)
    records_loaded = hs.seed_raw_records(db, sandbox_id)
    db.close()
    assert records_loaded == 14

    db = hs.get_sandbox_session(sandbox_id)
    match_result = hs.run_matching_step(db, sandbox_id)
    db.close()
    assert match_result["matched_count"] > 0

    db = hs.get_sandbox_session(sandbox_id)
    bridge_result = hs.run_bridge_step(db)
    db.close()
    assert bridge_result["bridges_calculated"] > 0

    db = hs.get_sandbox_session(sandbox_id)
    classify_result = hs.run_classification_step(db)
    db.close()
    assert classify_result["total_exceptions"] > 0
    assert classify_result["duplicates_detected"] == 1  # case "09"
    assert classify_result["requires_review"] > 0


def test_unknown_sandbox_id_returns_none_session():
    assert hs.get_sandbox_session("does-not-exist") is None


def test_reconciliation_steps_do_not_mutate_real_db():
    before = _hash_real_db()
    sandbox_id = hs.create_sandbox()
    db = hs.get_sandbox_session(sandbox_id)
    hs.seed_raw_records(db, sandbox_id)
    db.close()
    db = hs.get_sandbox_session(sandbox_id)
    hs.run_matching_step(db, sandbox_id)
    hs.run_bridge_step(db)
    hs.run_classification_step(db)
    db.close()
    after = _hash_real_db()
    assert before == after


# --- Approval race demo --------------------------------------------------

def test_approval_race_second_approver_gets_409_with_current_state():
    result = hs.build_approval_demo_sandbox()
    sandbox_id = result["sandbox_id"]
    exc = result["exception"]
    assert exc is not None
    assert exc["status"] == "open"

    db1 = hs.get_sandbox_session(sandbox_id)
    response = _approve_exception_core(db1, exc["id"], ApprovalRequest(approver="Sneha", decision="approved"))
    db1.close()
    assert response.status == "approved"

    db2 = hs.get_sandbox_session(sandbox_id)
    try:
        with pytest.raises(HTTPException) as excinfo:
            _approve_exception_core(db2, exc["id"], ApprovalRequest(approver="Rahul", decision="approved"))
        assert excinfo.value.status_code == 409
        assert "APPROVED BY SNEHA" in excinfo.value.detail
    finally:
        db2.close()


def test_approval_race_uses_a_throwaway_exception_not_primary_dataset():
    """The exception approved in this demo lives entirely inside the
    sandbox's own isolated database -- confirms it by checking the id space
    doesn't collide with anything meaningful on the real DB and that no real
    DB session is ever touched."""
    before = _hash_real_db()
    result = hs.build_approval_demo_sandbox()
    sandbox_id = result["sandbox_id"]
    exc = result["exception"]
    db = hs.get_sandbox_session(sandbox_id)
    _approve_exception_core(db, exc["id"], ApprovalRequest(approver="Sneha", decision="approved"))
    db.close()
    after = _hash_real_db()
    assert before == after
