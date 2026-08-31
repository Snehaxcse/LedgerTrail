"""
Regression test for a real bug found live during a pre-merge regression
pass: _log_exception_created (app/exceptions.py) and _log_anomaly_created
(app/anomaly_detection.py) embedded the raw PAISE integer directly into an
AuditEvent's after_state JSON, with no conversion applied -- even though
that JSON is returned verbatim by GET /audit-trail and rendered directly in
the UI (AuditTrail.jsx). Result: the audit trail displayed e.g. "unexplained
Rs.79,934.00" for an exception whose actual amount was Rs.799.34 -- a 100x
display bug, the raw paise integer shown as if it were already rupees.

Fixed by applying app.money.paise_to_rupees() at that one boundary (the
JSON construction site) -- the same "convert only at the point a response is
built" principle every other API response already follows. See
app/money.py's docstring for the full account of why exceptions.py/
anomaly_detection.py otherwise correctly stay paise-native.
"""
import datetime
import json

from app import anomaly_detection, bridge, exceptions, matching, models


def _seed_missing_refund_batch(db):
    """A batch whose single order shows a settlement refund the order record
    doesn't reflect (order_refund=0, entry.refund=578683 paise = Rs.5786.83)
    -- deliberately a non-round paise amount so a x100 display bug wouldn't
    be masked by a coincidentally-identical rupee/paise numeral."""
    gross = 887161
    fee = 22179
    tax = 3992
    refund = 578683
    net = gross - refund - fee - tax
    settlement_date = datetime.date(2027, 2, 1)

    txn = models.BankTransaction(
        amount=net, date=settlement_date, reference="UTR-AUDIT-TEST",
        description="NEFT CR: HDFC BANK TEST",
    )
    db.add(txn)
    db.flush()

    batch = models.SettlementBatch(
        settlement_date=settlement_date, total_gross=gross, total_refunds=refund,
        total_fees=fee, total_tax=tax, total_net=net,
    )
    db.add(batch)
    db.flush()

    db.add(models.SettlementEntry(
        batch_id=batch.id, order_ref="AUDIT-TEST-01", gross_amount=gross,
        fee=fee, tax=tax, refund=refund, net_amount=net,
    ))
    db.add(models.OrderRecord(
        order_ref="AUDIT-TEST-01", amount=gross, status="completed",
        refund_amount=0, fee_amount=fee,
    ))
    db.commit()
    return batch.id


def test_exception_created_audit_event_amount_is_rupees_not_paise(scratch_db):
    batch_id = _seed_missing_refund_batch(scratch_db)
    matching.run_matching(scratch_db)
    bridge.compute_bridge(scratch_db)
    exceptions.classify_exceptions(scratch_db)

    exc = (
        scratch_db.query(models.ExceptionRecord)
        .filter(models.ExceptionRecord.batch_id == batch_id)
        .first()
    )
    assert exc is not None
    assert exc.classification == "MISSING_REFUND_RECORD"
    assert exc.unexplained_amount == 578683  # the DB column stays paise -- that's correct

    event = (
        scratch_db.query(models.AuditEvent)
        .filter(models.AuditEvent.action == "exception_created")
        .order_by(models.AuditEvent.id.desc())
        .first()
    )
    assert event is not None
    after = json.loads(event.after_state)
    assert after["unexplained_amount"] == 5786.83, (
        f"audit event should show rupees (5786.83), not raw paise: got {after['unexplained_amount']}"
    )


def test_anomaly_created_audit_event_amount_is_rupees_not_paise(scratch_db):
    """Unit-tests _log_anomaly_created directly with a controlled paise
    value -- reconstructing a full multi-batch statistical-outlier scenario
    just to exercise this one JSON-construction helper would be
    disproportionate to what's actually under test here."""
    anomaly_detection._log_anomaly_created(
        scratch_db, classification="SYSTEMIC_FEE_DRIFT",
        unexplained_amount=79934, requires_approval=True,
    )
    scratch_db.commit()

    event = (
        scratch_db.query(models.AuditEvent)
        .filter(models.AuditEvent.action == "exception_created")
        .order_by(models.AuditEvent.id.desc())
        .first()
    )
    assert event is not None
    after = json.loads(event.after_state)
    assert after["unexplained_amount"] == 799.34, (
        f"audit event should show rupees (799.34), not raw paise: got {after['unexplained_amount']}"
    )
