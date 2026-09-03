"""
Tests for the Razorpay-shaped ingestion adapter (app/razorpay_ingestion.py).
All against an isolated in-memory SQLite DB (never app.database.engine/
SessionLocal, never ledgertrail.db) -- same discipline as
tests/test_policy.py and tests/test_auto_resolution_approval.py. No AI/LLM
calls anywhere in this module; nothing here touches investigation_agent.
"""
import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.database import Base
from app.razorpay_ingestion import (
    DEMO_REPLAY_PAYLOAD,
    RazorpayBankCreditPayload,
    RazorpaySettlementEntryPayload,
    RazorpaySettlementEventPayload,
    ingest_razorpay_event,
)


def _isolated_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _clean_payload(source_event_id="event_test_001"):
    return RazorpaySettlementEventPayload(
        source_event_id=source_event_id,
        settlement_date=datetime.date(2026, 12, 1),
        total_gross=Decimal("10000.00"),
        total_refunds=Decimal("0.00"),
        total_fees=Decimal("250.00"),
        total_tax=Decimal("45.00"),
        total_net=Decimal("9705.00"),
        entries=[
            RazorpaySettlementEntryPayload(
                order_ref="TEST-0001", gross_amount=Decimal("10000.00"),
                fee=Decimal("250.00"), tax=Decimal("45.00"), refund=Decimal("0.00"),
                net_amount=Decimal("9705.00"),
            ),
        ],
        bank_credit=RazorpayBankCreditPayload(
            amount=Decimal("9705.00"), date=datetime.date(2026, 12, 1),
            reference="UTR_TEST_0001",
            description="NEFT CR: HDFC BANK UTR_TEST_0001 RAZORPAY SETTLEMENT",
        ),
    )


def test_invalid_schema_missing_required_field_raises_validation_error():
    with pytest.raises(ValidationError):
        RazorpaySettlementEventPayload(
            settlement_date=datetime.date(2026, 12, 1),
            total_gross=Decimal("100"),
            total_refunds=Decimal("0"),
            total_fees=Decimal("1"),
            total_tax=Decimal("1"),
            total_net=Decimal("98"),
            entries=[],
            bank_credit=RazorpayBankCreditPayload(
                amount=Decimal("98"), date=datetime.date(2026, 12, 1), reference="X",
            ),
        )  # missing source_event_id


def test_invalid_schema_empty_entries_rejected():
    with pytest.raises(ValidationError):
        RazorpaySettlementEventPayload(
            source_event_id="event_empty",
            settlement_date=datetime.date(2026, 12, 1),
            total_gross=Decimal("100"), total_refunds=Decimal("0"),
            total_fees=Decimal("1"), total_tax=Decimal("1"), total_net=Decimal("98"),
            entries=[],
            bank_credit=RazorpayBankCreditPayload(
                amount=Decimal("98"), date=datetime.date(2026, 12, 1), reference="X",
            ),
        )


def test_first_ingestion_creates_batch_exact_paise_normalization():
    db = _isolated_db()
    try:
        result = ingest_razorpay_event(db, _clean_payload())
        assert result.duplicate is False
        assert result.ingested is True
        assert result.reconciled is True
        assert result.exceptions_created == []

        batch = db.query(models.SettlementBatch).filter(models.SettlementBatch.id == result.batch_id).first()
        assert batch is not None
        # Exact paise, not float-rounded: 9705.00 rupees -> 970500 paise.
        assert batch.total_net == 970500
        assert batch.total_gross == 1000000
        assert batch.total_fees == 25000
        assert batch.total_tax == 4500

        entries = db.query(models.SettlementEntry).filter(models.SettlementEntry.batch_id == batch.id).all()
        assert len(entries) == 1
        assert entries[0].net_amount == 970500

        txn = db.query(models.BankTransaction).filter(models.BankTransaction.reference == "UTR_TEST_0001").first()
        assert txn is not None
        assert txn.amount == 970500
        assert batch.bank_transaction_id == txn.id  # matching engine ran and matched it

        event = db.query(models.IngestedEvent).filter(models.IngestedEvent.source_event_id == "event_test_001").first()
        assert event is not None
        assert event.batch_id == batch.id
    finally:
        db.close()


def test_duplicate_ingestion_returns_existing_batch_no_new_financial_records():
    db = _isolated_db()
    try:
        first = ingest_razorpay_event(db, _clean_payload("event_dup_test"))
        assert first.duplicate is False

        batches_before = db.query(models.SettlementBatch).count()
        entries_before = db.query(models.SettlementEntry).count()
        txns_before = db.query(models.BankTransaction).count()
        events_before = db.query(models.IngestedEvent).count()

        second = ingest_razorpay_event(db, _clean_payload("event_dup_test"))
        assert second.duplicate is True
        assert second.ingested is False
        assert second.batch_id == first.batch_id

        assert db.query(models.SettlementBatch).count() == batches_before
        assert db.query(models.SettlementEntry).count() == entries_before
        assert db.query(models.BankTransaction).count() == txns_before
        assert db.query(models.IngestedEvent).count() == events_before
    finally:
        db.close()


def test_unique_constraint_enforced_at_database_level_direct_bypass():
    """Mirrors the HeldOutIngestionRecord precedent (app/holdout_sandbox.py):
    prove the uniqueness is a real DB constraint, not just an
    application-level check a caller could race past -- insert a second
    IngestedEvent row with the same source_event_id directly, bypassing
    ingest_razorpay_event entirely, and confirm the database itself refuses it."""
    db = _isolated_db()
    try:
        db.add(models.IngestedEvent(
            source_event_id="event_bypass", raw_payload="{}", batch_id=None,
            ingested_at=datetime.datetime.now(),
        ))
        db.commit()

        db.add(models.IngestedEvent(
            source_event_id="event_bypass", raw_payload="{}", batch_id=None,
            ingested_at=datetime.datetime.now(),
        ))
        with pytest.raises(Exception):  # sqlalchemy.exc.IntegrityError
            db.commit()
    finally:
        db.rollback()
        db.close()


def test_ingestion_does_not_disturb_existing_batches_exceptions_or_matches():
    """The critical regression-safety property: ingesting a new event must
    never touch any pre-existing batch/match/exception -- this is what the
    batch_ids scoping added to matching.run_matching / exceptions.classify_exceptions
    exists to guarantee. Seeds a pre-existing 'demo staging' exception
    (approved, like the real primary dataset's exception 1) and confirms it
    survives an ingestion completely unchanged."""
    db = _isolated_db()
    try:
        old_batch = models.SettlementBatch(
            settlement_date=datetime.date(2026, 1, 1),
            total_gross=10000, total_refunds=0, total_fees=100, total_tax=50, total_net=9850,
        )
        old_txn = models.BankTransaction(
            amount=9000, date=datetime.date(2026, 1, 1), reference="OLD-REF", description=None,
        )
        db.add_all([old_batch, old_txn])
        db.flush()
        # Directly assigned (not via matching.run_matching, whose 100-paise
        # tolerance wouldn't accept this deliberately-mismatched pair) --
        # what's under test is whether ingest_razorpay_event's own scoped
        # matching call disturbs an already-set link, not whether these two
        # rows would match on their own.
        old_batch.bank_transaction_id = old_txn.id
        old_exc = models.ExceptionRecord(
            batch_id=old_batch.id, unexplained_amount=850, classification="UNEXPLAINED_VARIANCE",
            suggested_action="pre-existing", status="approved", severity="high",
        )
        db.add(old_exc)
        db.commit()
        # NOTE: deliberately does NOT call the unscoped classify_exceptions(db)
        # here -- that would itself wipe-and-recreate old_exc (batch_ids=None
        # is a full-table wipe by design), destroying the very "approved"
        # state this test exists to prove survives ingestion. old_exc is
        # created directly above to represent that pre-existing state.
        old_exc_id = old_exc.id
        old_batch_id = old_batch.id

        pre_exceptions = {(e.id, e.status) for e in db.query(models.ExceptionRecord).all()}
        pre_matches = {(m.settlement_batch_id, m.bank_transaction_id) for m in db.query(models.Match).all()}

        result = ingest_razorpay_event(db, _clean_payload("event_isolation_test"))
        assert result.duplicate is False

        post_exceptions = {(e.id, e.status) for e in db.query(models.ExceptionRecord).all()}
        post_matches = {(m.settlement_batch_id, m.bank_transaction_id) for m in db.query(models.Match).all()}

        assert pre_exceptions.issubset(post_exceptions)
        assert pre_matches.issubset(post_matches)

        refreshed_old_exc = db.query(models.ExceptionRecord).filter(models.ExceptionRecord.id == old_exc_id).first()
        assert refreshed_old_exc.status == "approved"

        refreshed_old_batch = db.query(models.SettlementBatch).filter(models.SettlementBatch.id == old_batch_id).first()
        assert refreshed_old_batch.bank_transaction_id == old_txn.id
    finally:
        db.close()


def test_demo_replay_payload_is_valid_and_reconciles_cleanly():
    db = _isolated_db()
    try:
        result = ingest_razorpay_event(db, DEMO_REPLAY_PAYLOAD)
        assert result.duplicate is False
        assert result.reconciled is True
        assert result.exceptions_created == []
    finally:
        db.close()
