"""
FastAPI layer over the existing reconciliation logic. Read-only except for
POST /exceptions/{id}/approve. No matching/bridge/exception computation happens
here -- endpoints only read rows already written by scripts/run_reconciliation.py
(and record human approval decisions), so hitting the API never re-runs
classification and never discards a prior approval.
"""
import datetime
import json
import logging
from pathlib import Path
from typing import Any, List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app import bridge, models
from app.ai_explain import generate_explanation
from app.database import get_db, ensure_schema
from app.exceptions import CLASSIFICATION_INFO
from app.nl_query import answer_query

# Without this, "ledgertrail.ai_explain"'s path=ai_generated/path=fallback logs
# (an explicit requirement, to track how often the fallback triggers) have no
# handler attached under uvicorn and are silently dropped -- discovered while
# testing, since a fresh (non-cached) call produced no log output at all.
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

app = FastAPI(title="LedgerTrail")
ensure_schema()

VARIANCE_TOLERANCE = bridge.VARIANCE_TOLERANCE

GROUND_TRUTH_PATH = Path(__file__).resolve().parent.parent / "data" / "ground_truth.json"

# ground_truth.json's error "type" strings (chosen by the synthetic data generator)
# are a different naming convention from ExceptionRecord.classification (chosen by
# the classification engine) -- this mapping is the only place the two are tied
# together. Hardcoded, never AI-generated, same as CLASSIFICATION_INFO.
GROUND_TRUTH_TYPE_TO_CLASSIFICATION = {
    "missing_refund": "MISSING_REFUND_RECORD",
    "wrong_fee_tier": "FEE_TIER_MISMATCH",
    "duplicate_entry": "DUPLICATE_ENTRY",
    "timing_mismatch": "TIMING_DIFFERENCE",
}


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
    approver: Optional[str] = None
    reason: Optional[str] = None


class EvidenceOut(BaseModel):
    settlement_entries: List[SettlementEntryOut]
    order_records: List[OrderRecordOut]
    bank_transactions: List[BankTransactionOut]


class ApprovalRequest(BaseModel):
    approver: str
    decision: Literal["approved", "rejected"]
    reason: Optional[str] = None


class ApprovalResponse(BaseModel):
    exception_id: int
    status: str
    approval_log_id: int
    reason: Optional[str] = None


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


class PlantedErrorOut(BaseModel):
    type: str
    batch_id: int
    order_ref: Optional[str]
    expected_value: Any
    actual_value: Any
    detected: bool
    detected_classification: Optional[str]


class TransparencySummary(BaseModel):
    total_planted: int
    total_detected: int
    false_positives: int
    detection_rate: str


class TransparencyResponse(BaseModel):
    planted_errors: List[PlantedErrorOut]
    summary: TransparencySummary


class ExplainResponse(BaseModel):
    exception_id: int
    explanation: str
    source: Literal["ai_generated", "fallback"]
    # Additive beyond the spec's two-value source field, purely for test observability
    # (per "so we can see this during testing") -- distinguishes a fresh ai_generated
    # call from one served out of the ai_explanation cache without changing what
    # `source` means. Flagging this since it wasn't explicitly requested.
    cached: bool


class QueryRequest(BaseModel):
    question: str


class QueryResponse(BaseModel):
    answer: str
    source: Literal["answered", "unverifiable", "out_of_scope"]


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

    latest_log_by_exception = {}
    if rows:
        logs = (
            db.query(models.ApprovalLog)
            .filter(models.ApprovalLog.exception_id.in_([e.id for e in rows]))
            .order_by(models.ApprovalLog.id.desc())
            .all()
        )
        for log in logs:
            if log.exception_id not in latest_log_by_exception:
                latest_log_by_exception[log.exception_id] = log

    out = []
    for e in rows:
        info = CLASSIFICATION_INFO.get(e.classification, {})
        log = latest_log_by_exception.get(e.id)
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
                approver=log.approver if log else None,
                reason=log.reason if log else None,
            )
        )
    return out


def _extract_evidence_ids(linked_evidence_ids_json: Optional[str]):
    """Parses one ExceptionRecord's linked_evidence_ids JSON into typed id sets."""
    entry_ids, order_ids, bank_ids = set(), set(), set()
    for item in (json.loads(linked_evidence_ids_json) if linked_evidence_ids_json else []):
        if item["type"] == "settlement_entry":
            entry_ids.add(item["id"])
        elif item["type"] == "order_record":
            order_ids.add(item["id"])
        elif item["type"] == "bank_transaction":
            bank_ids.add(item["id"])
    return entry_ids, order_ids, bank_ids


def _resolve_evidence(db: Session, entry_ids, order_ids, bank_ids) -> EvidenceOut:
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


@app.get("/batches/{batch_id}/evidence", response_model=EvidenceOut)
def get_batch_evidence(batch_id: int, db: Session = Depends(get_db)):
    _get_batch_or_404(db, batch_id)
    exception_rows = (
        db.query(models.ExceptionRecord).filter(models.ExceptionRecord.batch_id == batch_id).all()
    )

    entry_ids, order_ids, bank_ids = set(), set(), set()
    for e in exception_rows:
        e_entry_ids, e_order_ids, e_bank_ids = _extract_evidence_ids(e.linked_evidence_ids)
        entry_ids |= e_entry_ids
        order_ids |= e_order_ids
        bank_ids |= e_bank_ids

    return _resolve_evidence(db, entry_ids, order_ids, bank_ids)


@app.post("/exceptions/{exception_id}/approve", response_model=ApprovalResponse)
def approve_exception(exception_id: int, body: ApprovalRequest, db: Session = Depends(get_db)):
    exc = db.query(models.ExceptionRecord).filter(models.ExceptionRecord.id == exception_id).first()
    if exc is None:
        raise HTTPException(status_code=404, detail=f"ExceptionRecord {exception_id} not found")

    reason = body.reason.strip() if body.reason else None
    if body.decision == "rejected" and not reason:
        raise HTTPException(status_code=400, detail="reason is required when decision is 'rejected'")

    before_status = exc.status
    exc.status = body.decision

    resulting_action = f"status set to '{body.decision}'"
    approval_log = models.ApprovalLog(
        exception_id=exc.id,
        approver=body.approver,
        decision=body.decision,
        reason=reason,
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
                {
                    "exception_id": exc.id,
                    "status": exc.status,
                    "approver": body.approver,
                    "decision": body.decision,
                    "reason": reason,
                }
            ),
        )
    )

    db.commit()

    return ApprovalResponse(
        exception_id=exc.id,
        status=exc.status,
        approval_log_id=approval_log.id,
        reason=reason,
    )


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


@app.get("/transparency", response_model=TransparencyResponse)
def get_transparency(db: Session = Depends(get_db)):
    """Compares the synthetic dataset's planted errors (ground_truth.json) against
    what the classification engine actually recorded in ExceptionRecord right now.
    Both sides are read fresh on every call -- nothing here is cached or hardcoded,
    so this number is only ever as good as the current data and current logic."""
    if not GROUND_TRUTH_PATH.exists():
        raise HTTPException(status_code=500, detail=f"ground_truth.json not found at {GROUND_TRUTH_PATH}")

    with open(GROUND_TRUTH_PATH, encoding="utf-8") as f:
        ground_truth = json.load(f)

    all_exceptions = db.query(models.ExceptionRecord).all()
    exceptions_by_batch = {}
    for e in all_exceptions:
        exceptions_by_batch.setdefault(e.batch_id, []).append(e)

    planted_errors_out = []
    expected_pairs = set()  # (batch_id, classification) pairs a planted error accounts for

    for entry in ground_truth:
        raw_type = entry["type"]
        expected_classification = GROUND_TRUTH_TYPE_TO_CLASSIFICATION.get(raw_type)
        batch_id = entry["batch_id"]

        detected = False
        detected_classification = None
        if expected_classification is not None:
            expected_pairs.add((batch_id, expected_classification))
            for e in exceptions_by_batch.get(batch_id, []):
                if e.classification == expected_classification:
                    detected = True
                    detected_classification = e.classification
                    break

        planted_errors_out.append(
            PlantedErrorOut(
                type=expected_classification or raw_type,
                batch_id=batch_id,
                order_ref=entry.get("order_ref"),
                expected_value=entry.get("expected_value"),
                actual_value=entry.get("actual_value"),
                detected=detected,
                detected_classification=detected_classification,
            )
        )

    total_planted = len(ground_truth)
    total_detected = sum(1 for p in planted_errors_out if p.detected)
    false_positives = sum(1 for e in all_exceptions if (e.batch_id, e.classification) not in expected_pairs)

    summary = TransparencySummary(
        total_planted=total_planted,
        total_detected=total_detected,
        false_positives=false_positives,
        detection_rate=f"{total_detected}/{total_planted}",
    )

    return TransparencyResponse(planted_errors=planted_errors_out, summary=summary)


@app.get("/batches/{batch_id}/exceptions/{exception_id}/explain", response_model=ExplainResponse)
def explain_exception(batch_id: int, exception_id: int, db: Session = Depends(get_db)):
    _get_batch_or_404(db, batch_id)
    exc = (
        db.query(models.ExceptionRecord)
        .filter(models.ExceptionRecord.id == exception_id, models.ExceptionRecord.batch_id == batch_id)
        .first()
    )
    if exc is None:
        raise HTTPException(
            status_code=404, detail=f"ExceptionRecord {exception_id} not found on batch {batch_id}"
        )

    if exc.ai_explanation:
        return ExplainResponse(
            exception_id=exc.id, explanation=exc.ai_explanation, source="ai_generated", cached=True
        )

    entry_ids, order_ids, bank_ids = _extract_evidence_ids(exc.linked_evidence_ids)
    evidence = _resolve_evidence(db, entry_ids, order_ids, bank_ids)

    exception_record = {
        "classification": exc.classification,
        "unexplained_amount": exc.unexplained_amount,
        "suggested_action": exc.suggested_action,
    }
    result = generate_explanation(exception_record, evidence.model_dump())

    # Only cache a validated AI response. A fallback is never cached, so a transient
    # API failure or a rejected response gets retried on the next call instead of
    # being locked in permanently.
    if result.source == "ai_generated":
        exc.ai_explanation = result.text
        db.commit()

    return ExplainResponse(exception_id=exc.id, explanation=result.text, source=result.source, cached=False)


@app.post("/query", response_model=QueryResponse)
def query(body: QueryRequest, db: Session = Depends(get_db)):
    """No caching by design: free-text questions won't repeat meaningfully, so
    every call gathers fresh context and calls the model fresh."""
    batches = list_batches(db)

    all_exceptions = []
    for batch in batches:
        for exc in get_batch_exceptions(batch.id, db):
            all_exceptions.append(
                {
                    "batch_id": exc.batch_id,
                    "classification": exc.classification,
                    "unexplained_amount": exc.unexplained_amount,
                    "status": exc.status,
                    "suggested_action": exc.suggested_action,
                }
            )

    # Precomputed in plain Python, not by the AI -- lets answer_query correctly
    # answer aggregate questions ("what's the total unexplained amount") using a
    # number this code already calculated and verified, instead of either
    # refusing or (worse) calculating it itself, which the system prompt forbids.
    total_unexplained_amount = round(
        sum(e["unexplained_amount"] for e in all_exceptions if e["status"] == "open"), 2
    )

    context_data = {
        "batches": [b.model_dump() for b in batches],
        "exceptions": all_exceptions,
        "total_unexplained_amount": total_unexplained_amount,
    }

    result = answer_query(body.question, context_data)
    return QueryResponse(answer=result.text, source=result.source)
