"""
Persisted regression tests for Phase B's bounded tool layer
(app/investigation_tools.py). Read-only against the REAL ledgertrail.db --
these tools have no db.add()/db.commit() anywhere by design, and this file
also proves it empirically rather than just by code inspection.
"""
from app import investigation_tools as tools
from app import models
from app.database import SessionLocal


def _db():
    return SessionLocal()


def test_get_settlement_batch_known_values():
    db = _db()
    try:
        b = tools.get_settlement_batch(db, 2)
        assert b["total_net"] == 116294.35
        assert b["is_matched"] is True
        assert tools.get_settlement_batch(db, 999999) is None
    finally:
        db.close()


def test_get_settlement_entries_matches_missing_refund_case():
    db = _db()
    try:
        entries = tools.get_settlement_entries(db, 2)
        entry = next(e for e in entries if e["order_ref"] == "OD-02-0001")
        assert entry["refund"] == 5786.83
    finally:
        db.close()


def test_get_bank_candidates_exact_and_fuzzy():
    db = _db()
    try:
        exact = tools.get_bank_candidates(db, 2)
        assert len(exact["candidates"]) == 1
        assert exact["candidates"][0]["match_type"] == "exact"
        assert exact["candidates"][0]["is_current_match"] is True

        fuzzy = tools.get_bank_candidates(db, 4)
        assert fuzzy["candidates"][0]["match_type"] == "fuzzy"
        assert fuzzy["candidates"][0]["date_diff_days"] == 2
        assert fuzzy["candidates"][0]["confidence_score"] == 0.85
    finally:
        db.close()


def test_get_order_and_get_refunds_use_order_ref_not_numeric_id():
    db = _db()
    try:
        order = tools.get_order(db, "OD-02-0001")
        assert order["refund_amount"] == 0.0
        assert order["fee_amount"] == 221.79

        refunds = tools.get_refunds(db, "OD-02-0001")
        assert refunds["order_record_refund"] == 0.0
        assert refunds["settlement_entry_refunds"][0]["refund"] == 5786.83

        assert tools.get_order(db, "NOT-A-REAL-REF") is None
    finally:
        db.close()


def test_get_exception_evidence_record_references_vs_anomaly_comparison():
    db = _db()
    try:
        missing_refund = tools.get_exception_evidence(db, 1)
        assert missing_refund["evidence_type"] == "record_references"
        assert missing_refund["unexplained_amount"] == 5786.83
        assert len(missing_refund["settlement_entries"]) == 1

        anomaly = tools.get_exception_evidence(db, 7)
        assert anomaly["evidence_type"] == "anomaly_comparison"
        assert anomaly["comparison"]["metric"] == "fee_rate"
        assert anomaly["comparison"]["deviation_stdevs"] == 77.2
        assert anomaly["comparison"]["baseline_batches"] == [1, 7, 8, 10]
    finally:
        db.close()


def test_calculate_bridge_reuses_real_bridge_module():
    db = _db()
    try:
        result = tools.calculate_bridge(db, 4)
        assert result["variance"] == 0.0
        assert result["is_reconciled"] is True
        assert tools.calculate_bridge(db, 999999) is None
    finally:
        db.close()


def test_verify_amount_relationship_is_pure_arithmetic_no_ai():
    result = tools.verify_amount_relationship(5786.83, 0.0, "entry.refund", "order.refund_amount")
    assert result["difference"] == 5786.83
    assert result["within_tolerance"] is False

    equal = tools.verify_amount_relationship(100.0, 100.0, "a", "b")
    assert equal["within_tolerance"] is True


def test_verify_reference_relationship_substring_match():
    found = tools.verify_reference_relationship(
        "UTR882547985119", "NEFT CR: HDFC BANK UTR882547985119 RAZORPAY SETTLEMENT"
    )
    assert found["found"] is True

    not_found = tools.verify_reference_relationship("UTR000000000000", "some unrelated narration")
    assert not_found["found"] is False


def test_verify_narration_wraps_the_real_ai_feature():
    """Live Anthropic call -- confirms the wrapper builds the right dict and
    passes it through to the existing, already-cross-verified feature."""
    db = _db()
    try:
        result = tools.verify_narration(db, 9)
        assert result is not None
        assert result.is_settlement_credit is True
        assert result.source in ("ai_verified", "fallback")
        assert tools.verify_narration(db, 999999) is None
    finally:
        db.close()


def test_tool_layer_does_not_mutate_the_real_database():
    """THE checkpoint for this phase, same discipline as Phase A: every tool
    called against real, known data must have zero observable effect."""
    before = _db()
    try:
        before_state = before.query(models.ExceptionRecord).order_by(models.ExceptionRecord.id).all()
        before_snapshot = [(e.id, e.status, e.classification) for e in before_state]
    finally:
        before.close()

    db = _db()
    try:
        tools.get_settlement_batch(db, 2)
        tools.get_settlement_entries(db, 2)
        tools.get_bank_candidates(db, 4)
        tools.get_order(db, "OD-02-0001")
        tools.get_refunds(db, "OD-02-0001")
        tools.get_exception_evidence(db, 1)
        tools.get_exception_evidence(db, 7)
        tools.calculate_bridge(db, 9)
        tools.verify_narration(db, 9)
    finally:
        db.close()

    after = _db()
    try:
        after_state = after.query(models.ExceptionRecord).order_by(models.ExceptionRecord.id).all()
        after_snapshot = [(e.id, e.status, e.classification) for e in after_state]
    finally:
        after.close()

    assert before_snapshot == after_snapshot
