from sqlalchemy import Column, Integer, String, Float, Date, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship, backref

from app.database import Base


class SettlementBatch(Base):
    __tablename__ = "settlement_batches"

    id = Column(Integer, primary_key=True)
    settlement_date = Column(Date, nullable=False)
    # Paise (integer), not decimal rupees -- see float-to-paise migration Phase 1.
    total_gross = Column(Integer, nullable=False)
    total_refunds = Column(Integer, nullable=False)
    total_fees = Column(Integer, nullable=False)
    total_tax = Column(Integer, nullable=False)
    total_net = Column(Integer, nullable=False)
    bank_transaction_id = Column(Integer, ForeignKey("bank_transactions.id"), unique=True, nullable=True)

    bank_transaction = relationship("BankTransaction", backref=backref("matched_batch", uselist=False))


class SettlementEntry(Base):
    __tablename__ = "settlement_entries"

    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("settlement_batches.id"), nullable=False)
    order_ref = Column(String, nullable=False, index=True)
    # Paise (integer), not decimal rupees -- see float-to-paise migration Phase 1.
    gross_amount = Column(Integer, nullable=False)
    fee = Column(Integer, nullable=False)
    tax = Column(Integer, nullable=False)
    refund = Column(Integer, nullable=False, default=0)
    net_amount = Column(Integer, nullable=False)


class BankTransaction(Base):
    __tablename__ = "bank_transactions"

    id = Column(Integer, primary_key=True)
    # Paise (integer), not decimal rupees -- see float-to-paise migration Phase 1.
    amount = Column(Integer, nullable=False)
    date = Column(Date, nullable=False)
    reference = Column(String, nullable=False, index=True)
    description = Column(String, nullable=True)


class OrderRecord(Base):
    __tablename__ = "order_records"

    id = Column(Integer, primary_key=True)
    order_ref = Column(String, nullable=False, index=True)
    # Paise (integer), not decimal rupees -- see float-to-paise migration Phase 1.
    amount = Column(Integer, nullable=False)
    status = Column(String, nullable=False)
    refund_amount = Column(Integer, nullable=True)
    fee_amount = Column(Integer, nullable=False)


class Match(Base):
    __tablename__ = "matches"

    id = Column(Integer, primary_key=True)
    settlement_batch_id = Column(Integer, ForeignKey("settlement_batches.id"), nullable=False)
    bank_transaction_id = Column(Integer, ForeignKey("bank_transactions.id"), nullable=False)
    confidence_score = Column(Float, nullable=False)
    match_type = Column(String, nullable=False)  # "exact" | "fuzzy"

    settlement_batch = relationship("SettlementBatch")
    bank_transaction = relationship("BankTransaction")


class ExceptionRecord(Base):
    __tablename__ = "exceptions"

    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("settlement_batches.id"), nullable=False)
    # Paise (integer), not decimal rupees -- see float-to-paise migration Phase 1.
    unexplained_amount = Column(Integer, nullable=False)
    classification = Column(String, nullable=False)
    suggested_action = Column(String, nullable=True)
    status = Column(String, nullable=False, default="open")  # "open" | "approved" | "rejected"
    linked_evidence_ids = Column(Text, nullable=True)  # JSON-encoded list of related record ids
    ai_explanation = Column(Text, nullable=True)  # cached validated AI explanation; never caches a fallback
    severity = Column(String, nullable=True)  # "high" | "medium" | "low" | "info"; computed at classification time
    investigation_result = Column(Text, nullable=True)  # cached JSON InvestigationOut; never caches source="fallback"

    batch = relationship("SettlementBatch")


class ApprovalLog(Base):
    __tablename__ = "approval_logs"

    id = Column(Integer, primary_key=True)
    exception_id = Column(Integer, ForeignKey("exceptions.id"), nullable=False)
    approver = Column(String, nullable=False)
    decision = Column(String, nullable=False)
    reason = Column(String, nullable=True)  # required on reject; enforced in the approve endpoint
    timestamp = Column(DateTime, nullable=False)
    resulting_action = Column(String, nullable=True)

    exception = relationship("ExceptionRecord")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, nullable=False)
    actor = Column(String, nullable=False)  # "system" | "AI" | "human"
    action = Column(String, nullable=False)
    before_state = Column(Text, nullable=True)  # JSON-encoded snapshot
    after_state = Column(Text, nullable=True)  # JSON-encoded snapshot


class DemoUser(Base):
    """Minimal, deliberately small auth model -- username + password (hashed,
    see app/auth.py) -> role. Two roles only: "analyst" (view/investigate,
    cannot approve/reject) and "approver" (everything an analyst can do,
    plus approve/reject). Seeded idempotently at boot (app/startup.py) --
    NOT wiped on regen (unlike ApprovalLog/IngestedEvent): user accounts
    aren't part of the reconciliation dataset's reseed lifecycle, and
    re-creating them on every restart would needlessly invalidate any
    still-open session. display_name is what's recorded as the actor in
    ApprovalLog/AuditEvent -- server-derived from the session, never from
    client-supplied request data (see app/main.py's approve_exception)."""
    __tablename__ = "demo_users"

    id = Column(Integer, primary_key=True)
    username = Column(String, nullable=False, unique=True)
    password_hash = Column(String, nullable=False)
    password_salt = Column(String, nullable=False)
    role = Column(String, nullable=False)  # "analyst" | "approver"
    display_name = Column(String, nullable=False)
    job_title = Column(String, nullable=False)


class UserSession(Base):
    """Server-issued opaque session token (app/auth.py) -- deliberately NOT a
    JWT/refresh-token scheme (see spec: "keep this extremely small"). No
    expiry logic: this is a synthetic demo credential set, not a production
    security boundary. token is a cryptographically random 32-byte urlsafe
    string (secrets.token_urlsafe), unguessable in practice; looked up
    directly per request rather than decoded/verified, so revocation is a
    simple row delete (see /auth/logout)."""
    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True)
    token = Column(String, nullable=False, unique=True)
    user_id = Column(Integer, ForeignKey("demo_users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False)


class IngestedEvent(Base):
    """Idempotency + provenance record for the REAL Razorpay-shaped ingestion
    path (app/razorpay_ingestion.py) -- unlike HeldOutIngestionRecord below,
    this DOES run against the primary ledgertrail.db and DOES create a real
    SettlementBatch a judge sees in the normal batch list. Same proven
    unique-constraint idempotency pattern: a second ingestion attempt with the
    same source_event_id fails at the database level (IntegrityError caught
    by the adapter), not via an application-code check a caller could race
    past. raw_payload is the validated, as-received payload (JSON-encoded) --
    kept for provenance/debugging, never re-parsed for reconciliation (the
    batch/entries/bank transaction rows it produced are the source of truth
    once created)."""
    __tablename__ = "ingested_events"

    id = Column(Integer, primary_key=True)
    source_event_id = Column(String, nullable=False, unique=True)
    raw_payload = Column(Text, nullable=False)
    # Nullable: the event row is inserted (and its uniqueness constraint checked)
    # BEFORE the batch it produces exists -- see app/razorpay_ingestion.py -- then
    # backfilled in the same transaction once the batch is created.
    batch_id = Column(Integer, ForeignKey("settlement_batches.id"), nullable=True)
    ingested_at = Column(DateTime, nullable=False)


class HeldOutIngestionRecord(Base):
    """Idempotency tracking for the held-out sandbox's replay demo
    (app/holdout_sandbox.py) -- NOT part of the primary reconciliation
    pipeline and never populated on the real ledgertrail.db (nothing in
    app/startup.py or scripts/ingest.py ever inserts into this table; the
    table exists there only because Base.metadata.create_all() creates every
    model's table on every engine, primary included). The unique constraint
    on source_event_id is what makes the idempotency demo real: a second
    ingestion attempt with the same source_event_id fails at the database
    level (IntegrityError), not via an application-code "does this already
    exist" check that a caller could bypass."""
    __tablename__ = "holdout_ingestion_records"

    id = Column(Integer, primary_key=True)
    source_event_id = Column(String, nullable=False, unique=True)
    ingested_at = Column(DateTime, nullable=False)
