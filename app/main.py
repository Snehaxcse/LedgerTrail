"""
FastAPI layer over the existing reconciliation logic. Read-only except for
POST /exceptions/{id}/approve. No matching/bridge/exception computation happens
here -- endpoints only read rows already written by scripts/run_reconciliation.py
(and record human approval decisions), so hitting the API never re-runs
classification and never discards a prior approval.
"""
import datetime
import json
from typing import List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app import bridge, models
from app.database import get_db
from app.exceptions import CLASSIFICATION_INFO

app = FastAPI(title="LedgerTrail")

VARIANCE_TOLERANCE = bridge.VARIANCE_TOLERANCE


# ---------- schemas ----------

class SettlementEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    order_ref: str
    gross_amount: float
    fee: float
    tax: float
    refund: float
    net_amount: float


class OrderRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    order_ref: str
    amount: float
    status: str
    refund_amount: Optional[float]
    fee_amount: float


class BankTransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    amount: float
    date: datetime.date
    reference: str
    description: Optional[str]


class BatchSummary(BaseModel):
    id: int
    settlement_date: datetime.date
    total_gross: float
    total_refunds: float
    total_fees: float
    total_tax: float
    total_net: float
    matched_bank_amount: Optional[float]
    match_type: Optional[str]
    confidence_score: Optional[float]
    is_reconciled: bool
    variance: Optional[float]


class BatchDetail(BatchSummary):
    entries: List[SettlementEntryOut]


class ExceptionOut(BaseModel):
    id: int
    batch_id: int
    classification: str
    unexplained_amount: float
    suggested_action: Optional[str]
    status: str
    requires_approval: bool
    linked_evidence_ids: List[dict]


class EvidenceOut(BaseModel):
    settlement_entries: List[SettlementEntryOut]
    order_records: List[OrderRecordOut]
    bank_transactions: List[BankTransactionOut]


class ApprovalRequest(BaseModel):
    approver: str
    decision: Literal["approved", "rejected"]


class ApprovalResponse(BaseModel):
    exception_id: int
    status: str
    approval_log_id: int


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    timestamp: datetime.datetime
    actor: str
    action: str
    before_state: Optional[str]
    after_state: Optional[str]


class AuditTrailResponse(BaseModel):
    items: List[AuditEventOut]
    total: int
    limit: int
    offset: int


# ---------- helpers ----------

def _match_for_batch(db: Session, batch_id: int) -> Optional[models.Match]:
    return (
        db.query(models.Match)
        .filter(models.Match.settlement_batch_id == batch_id)
        .order_by(models.Match.id.desc())
        .first()
    )


def _batch_summary(db: Session, batch: models.SettlementBatch) -> BatchSummary:
    match_row = _match_for_batch(db, batch.id)
    bank_amount = batch.bank_transaction.amount if batch.bank_transaction_id else None
    variance = round(batch.total_net - bank_amount, 2) if bank_amount is not None else None

    open_exceptions = (
        db.query(models.ExceptionRecord)
        .filter(models.ExceptionRecord.batch_id == batch.id, models.ExceptionRecord.status == "open")
        .all()
    )
    # An open exception keeps a batch un-reconciled if it requires a human decision
    # OR blocks reconciliation -- checking blocks_reconciliation alone isn't enough:
    # DUPLICATE_ENTRY has blocks_reconciliation=False but still requires_approval=True,
    # so it must still suppress is_reconciled. Only an exception where BOTH are False
    # (currently just TIMING_DIFFERENCE) can coexist with is_reconciled=True.
    has_outstanding_exception = any(
        CLASSIFICATION_INFO.get(e.classification, {}).get("requires_approval", True)
        or CLASSIFICATION_INFO.get(e.classification, {}).get("blocks_reconciliation", True)
        for e in open_exceptions
    )
    is_reconciled = (
        not has_outstanding_exception
        and variance is not None
        and abs(variance) <= VARIANCE_TOLERANCE
    )

    return BatchSummary(
        id=batch.id,
        settlement_date=batch.settlement_date,
        total_gross=batch.total_gross,
        total_refunds=batch.total_refunds,
        total_fees=batch.total_fees,
        total_tax=batch.total_tax,
        total_net=batch.total_net,
        matched_bank_amount=bank_amount,
        match_type=match_row.match_type if match_row else None,
        confidence_score=match_row.confidence_score if match_row else None,
        is_reconciled=is_reconciled,
        variance=variance,
    )


def _get_batch_or_404(db: Session, batch_id: int) -> models.SettlementBatch:
    batch = db.query(models.SettlementBatch).filter(models.SettlementBatch.id == batch_id).first()
    if batch is None:
        raise HTTPException(status_code=404, detail=f"SettlementBatch {batch_id} not found")
    return batch


# ---------- routes ----------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/batches", response_model=List[BatchSummary])
def list_batches(db: Session = Depends(get_db)):
    batches = db.query(models.SettlementBatch).order_by(models.SettlementBatch.id).all()
    return [_batch_summary(db, b) for b in batches]


@app.get("/batches/{batch_id}", response_model=BatchDetail)
def get_batch(batch_id: int, db: Session = Depends(get_db)):
    batch = _get_batch_or_404(db, batch_id)
    summary = _batch_summary(db, batch)
    entries = (
        db.query(models.SettlementEntry)
        .filter(models.SettlementEntry.batch_id == batch_id)
        .order_by(models.SettlementEntry.id)
        .all()
    )
    return BatchDetail(**summary.model_dump(), entries=entries)


@app.get("/batches/{batch_id}/exceptions", response_model=List[ExceptionOut])
def get_batch_exceptions(batch_id: int, db: Session = Depends(get_db)):
    _get_batch_or_404(db, batch_id)
    rows = (
        db.query(models.ExceptionRecord)
        .filter(models.ExceptionRecord.batch_id == batch_id)
        .order_by(models.ExceptionRecord.id)
        .all()
    )
    out = []
    for e in rows:
        info = CLASSIFICATION_INFO.get(e.classification, {})
        out.append(
            ExceptionOut(
                id=e.id,
                batch_id=e.batch_id,
                classification=e.classification,
                unexplained_amount=e.unexplained_amount,
                suggested_action=e.suggested_action,
                status=e.status,
                requires_approval=info.get("requires_approval", False),
                linked_evidence_ids=json.loads(e.linked_evidence_ids) if e.linked_evidence_ids else [],
            )
        )
    return out


@app.get("/batches/{batch_id}/evidence", response_model=EvidenceOut)
def get_batch_evidence(batch_id: int, db: Session = Depends(get_db)):
    _get_batch_or_404(db, batch_id)
    exception_rows = (
        db.query(models.ExceptionRecord).filter(models.ExceptionRecord.batch_id == batch_id).all()
    )

    entry_ids, order_ids, bank_ids = set(), set(), set()
    for e in exception_rows:
        for item in (json.loads(e.linked_evidence_ids) if e.linked_evidence_ids else []):
            if item["type"] == "settlement_entry":
                entry_ids.add(item["id"])
            elif item["type"] == "order_record":
                order_ids.add(item["id"])
            elif item["type"] == "bank_transaction":
                bank_ids.add(item["id"])

    entries = (
        db.query(models.SettlementEntry).filter(models.SettlementEntry.id.in_(entry_ids)).all()
        if entry_ids
        else []
    )
    orders = (
        db.query(models.OrderRecord).filter(models.OrderRecord.id.in_(order_ids)).all()
        if order_ids
        else []
    )
    bank_txns = (
        db.query(models.BankTransaction).filter(models.BankTransaction.id.in_(bank_ids)).all()
        if bank_ids
        else []
    )

    return EvidenceOut(settlement_entries=entries, order_records=orders, bank_transactions=bank_txns)


@app.post("/exceptions/{exception_id}/approve", response_model=ApprovalResponse)
def approve_exception(exception_id: int, body: ApprovalRequest, db: Session = Depends(get_db)):
    exc = db.query(models.ExceptionRecord).filter(models.ExceptionRecord.id == exception_id).first()
    if exc is None:
        raise HTTPException(status_code=404, detail=f"ExceptionRecord {exception_id} not found")

    before_status = exc.status
    exc.status = body.decision

    resulting_action = f"status set to '{body.decision}'"
    approval_log = models.ApprovalLog(
        exception_id=exc.id,
        approver=body.approver,
        decision=body.decision,
        timestamp=datetime.datetime.now(),
        resulting_action=resulting_action,
    )
    db.add(approval_log)
    db.flush()

    # AuditEvent rows are append-only: never update or delete an AuditEvent once written.
    db.add(
        models.AuditEvent(
            timestamp=datetime.datetime.now(),
            actor="human",
            action="exception_reviewed",
            before_state=json.dumps({"exception_id": exc.id, "status": before_status}),
            after_state=json.dumps(
                {"exception_id": exc.id, "status": exc.status, "approver": body.approver, "decision": body.decision}
            ),
        )
    )

    db.commit()

    return ApprovalResponse(exception_id=exc.id, status=exc.status, approval_log_id=approval_log.id)


@app.get("/audit-trail", response_model=AuditTrailResponse)
def get_audit_trail(limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    total = db.query(models.AuditEvent).count()
    items = (
        db.query(models.AuditEvent)
        .order_by(models.AuditEvent.timestamp.desc(), models.AuditEvent.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return AuditTrailResponse(items=items, total=total, limit=limit, offset=offset)
