from sqlalchemy import Column, Integer, String, Float, Date, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship, backref

from app.database import Base


class SettlementBatch(Base):
    __tablename__ = "settlement_batches"

    id = Column(Integer, primary_key=True)
    settlement_date = Column(Date, nullable=False)
    total_gross = Column(Float, nullable=False)
    total_refunds = Column(Float, nullable=False)
    total_fees = Column(Float, nullable=False)
    total_tax = Column(Float, nullable=False)
    total_net = Column(Float, nullable=False)
    bank_transaction_id = Column(Integer, ForeignKey("bank_transactions.id"), unique=True, nullable=True)

    bank_transaction = relationship("BankTransaction", backref=backref("matched_batch", uselist=False))


class SettlementEntry(Base):
    __tablename__ = "settlement_entries"

    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("settlement_batches.id"), nullable=False)
    order_ref = Column(String, nullable=False, index=True)
    gross_amount = Column(Float, nullable=False)
    fee = Column(Float, nullable=False)
    tax = Column(Float, nullable=False)
    refund = Column(Float, nullable=False, default=0.0)
    net_amount = Column(Float, nullable=False)


class BankTransaction(Base):
    __tablename__ = "bank_transactions"

    id = Column(Integer, primary_key=True)
    amount = Column(Float, nullable=False)
    date = Column(Date, nullable=False)
    reference = Column(String, nullable=False, index=True)
    description = Column(String, nullable=True)


class OrderRecord(Base):
    __tablename__ = "order_records"

    id = Column(Integer, primary_key=True)
    order_ref = Column(String, nullable=False, index=True)
    amount = Column(Float, nullable=False)
    status = Column(String, nullable=False)
    refund_amount = Column(Float, nullable=True)
    fee_amount = Column(Float, nullable=False)


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
    unexplained_amount = Column(Float, nullable=False)
    classification = Column(String, nullable=False)
    suggested_action = Column(String, nullable=True)
    status = Column(String, nullable=False, default="open")  # "open" | "approved" | "rejected"
    linked_evidence_ids = Column(Text, nullable=True)  # JSON-encoded list of related record ids

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
