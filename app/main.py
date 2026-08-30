"""
FastAPI layer over the existing reconciliation logic. Read-only except for
POST /exceptions/{id}/approve. No matching/bridge/exception computation happens
inside a REQUEST -- endpoints only read rows already written by the pipeline
(and record human approval decisions), so handling an API request never re-runs
classification and never discards a prior approval. The pipeline itself DOES
run once, automatically, on app boot (see app.startup.run_startup_sequence,
registered below) -- that's what guarantees a fresh deploy always starts from
the known-good demo state, independent of anything a previous visitor did.
"""
import datetime
import json
import logging
from pathlib import Path
from typing import Any, List, Literal, Optional, Union

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update
from sqlalchemy.orm import Session

from app import bridge, models
from app.ai_explain import generate_explanation
from app.anomaly_detection import ANOMALY_CLASSIFICATIONS
from app.database import get_db, ensure_schema
from app.exceptions import CLASSIFICATION_INFO
from app.matching import match_basis as _match_basis
from app.money import paise_to_rupees
from app.narration_verification import verify_narration
from app.nl_query import answer_query
from app.startup import run_startup_sequence

# Without this, "ledgertrail.ai_explain"'s path=ai_generated/path=fallback logs
# (an explicit requirement, to track how often the fallback triggers) have no
# handler attached under uvicorn and are silently dropped -- discovered while
# testing, since a fresh (non-cached) call produced no log output at all.
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

app = FastAPI(title="LedgerTrail")
ensure_schema()

# Restricted to the real deployed frontend now that its domain is known.
# allow_credentials=False is unaffected by this change -- this API still doesn't
# use cookies/credentials -- but a specific origin list is tighter than "*"
# regardless.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://ledger-trail-rho.vercel.app"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _on_startup():
    run_startup_sequence()

VARIANCE_TOLERANCE = bridge.VARIANCE_TOLERANCE

# An assumption, not a measurement: how long a human would plausibly spend manually
# finding+investigating ONE exception (locating the mismatched rows, cross-referencing
# order/bank records) if this system didn't surface it automatically. Purely for a
# labeled, honest ESTIMATE in GET /stats -- never presented as a measured/verified
# figure, and deliberately excluded from anywhere numbers are treated as ground truth
# (e.g. /transparency).
TIME_SAVED_MINUTES_PER_EXCEPTION = 15.0

# Fixed set of demo operator identities. This is NOT authentication -- it's a
# cheap closing of the "type literally anything" gap: POST /exceptions/{id}/approve
# now rejects any approver not in this dict (see approve_exception below), instead
# of accepting an arbitrary client-supplied string. Single source of truth: the
# frontend's dropdown is populated from GET /approvers (below) rather than keeping
# its own hardcoded copy of these names, so the two can't drift out of sync.
# "Sneha" must stay in this dict -- app/startup.py's DEMO_APPROVER stages the
# demo's "before" approval example under that exact name.
DEMO_APPROVERS = {
    "Sneha": "Finance Analyst",
    "Rahul": "Reconciliation Analyst",
    "Priya": "Settlements Lead",
}

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
    "systemic_fee_drift": "SYSTEMIC_FEE_DRIFT",
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
    confidence_score: Optional[float] = Field(
        None,
        description=(
            "A deterministic heuristic weight (1.0 for an exact same-day match, 0.85 "
            "for a fuzzy match within the date window) -- NOT a statistical confidence "
            "interval or probability. See match_basis for the same rule in plain language."
        ),
    )
    match_basis: Optional[str] = Field(
        None, description="Plain-language restatement of match_type/confidence_score."
    )
    is_reconciled: bool
    variance: Optional[float]


class BankStatementRow(BankTransactionOut):
    matched_batch_id: Optional[int] = None


class BatchDetail(BatchSummary):
    entries: List[SettlementEntryOut]
    bank_transaction: Optional[BankTransactionOut] = None


class ExceptionOut(BaseModel):
    id: int
    batch_id: int
    classification: str
    unexplained_amount: float
    suggested_action: Optional[str]
    status: str
    requires_approval: bool
    severity: Optional[str]
    linked_evidence_ids: List[dict]
    approver: Optional[str] = None
    reason: Optional[str] = None


class EvidenceOut(BaseModel):
    settlement_entries: List[SettlementEntryOut]
    order_records: List[OrderRecordOut]
    bank_transactions: List[BankTransactionOut]


# Evidence shape for SYSTEMIC_FEE_DRIFT / SYSTEMIC_REFUND_DRIFT: these classifications
# are an aggregate statistical comparison, not any single row, so their evidence is
# the comparison itself rather than resolved SettlementEntry/OrderRecord/BankTransaction
# rows. See app.anomaly_detection.run_anomaly_detection, which is what writes this shape.
class AnomalyEvidenceOut(BaseModel):
    metric: str  # "fee_rate" | "refund_rate"
    batch_value: float
    baseline_mean: float
    baseline_stdev: float
    deviation_stdevs: float
    relative_deviation_pct: float
    baseline_batches: List[int]


class ApprovalRequest(BaseModel):
    approver: str = Field(
        description="Must be one of the fixed demo names in GET /approvers -- checked "
        "server-side, rejected otherwise (see approve_exception). Still not real "
        "authentication: picking a name from a list proves nothing about who is "
        "actually calling this endpoint, it just closes the 'type literally anything' "
        "gap of a free-text field. Recorded as-is in ApprovalLog/AuditEvent."
    )
    decision: Literal["approved", "rejected"]
    reason: Optional[str] = None


class ApprovalResponse(BaseModel):
    exception_id: int
    status: str
    approval_log_id: int
    reason: Optional[str] = None


class DemoApproverOut(BaseModel):
    name: str
    role: str


class DataSourceOut(BaseModel):
    name: str
    format: str
    record_count: int
    description: str


class DataSourcesResponse(BaseModel):
    sources: List[DataSourceOut]
    note: str


class NarrationVerificationOut(BaseModel):
    bank_transaction_id: int
    is_settlement_credit: bool
    confidence_note: str
    source: Literal["ai_verified", "fallback"]


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


class TimeSavedEstimate(BaseModel):
    assumption: str
    minutes_per_exception: float
    total_exceptions: int
    estimated_minutes_saved: float
    estimated_hours_saved: float


class StatsResponse(BaseModel):
    total_batches: int
    batches_reconciled_automatically: int = Field(
        description="Batches with zero ExceptionRecord rows in the CURRENT classification "
        "pass -- never needed a human decision at all."
    )
    batches_requiring_review: int = Field(
        description="Batches with at least one ExceptionRecord row (any status: open, "
        "approved, or rejected) in the CURRENT pass -- needed at least one human decision."
    )
    time_saved: TimeSavedEstimate


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


class TrendEntry(BaseModel):
    settlement_date: datetime.date
    batch_id: int
    is_reconciled: bool
    variance: Optional[float]
    total_net: float


# ---------- helpers ----------

def _match_for_batch(db: Session, batch_id: int) -> Optional[models.Match]:
    return (
        db.query(models.Match)
        .filter(models.Match.settlement_batch_id == batch_id)
        .order_by(models.Match.id.desc())
        .first()
    )


# Explicit constructors for the three money-bearing sub-schemas, replacing reliance
# on from_attributes=True auto-mapping straight off the ORM object -- that would
# copy paise integers through untouched. paise_to_rupees() is applied here, at
# construction time, same boundary rule as everywhere else in this file.
def _settlement_entry_out(e: models.SettlementEntry) -> SettlementEntryOut:
    return SettlementEntryOut(
        id=e.id,
        order_ref=e.order_ref,
        gross_amount=paise_to_rupees(e.gross_amount),
        fee=paise_to_rupees(e.fee),
        tax=paise_to_rupees(e.tax),
        refund=paise_to_rupees(e.refund),
        net_amount=paise_to_rupees(e.net_amount),
    )


def _order_record_out(o: models.OrderRecord) -> OrderRecordOut:
    return OrderRecordOut(
        id=o.id,
        order_ref=o.order_ref,
        amount=paise_to_rupees(o.amount),
        status=o.status,
        refund_amount=paise_to_rupees(o.refund_amount),
        fee_amount=paise_to_rupees(o.fee_amount),
    )


def _bank_transaction_out(t: models.BankTransaction) -> BankTransactionOut:
    return BankTransactionOut(
        id=t.id,
        amount=paise_to_rupees(t.amount),
        date=t.date,
        reference=t.reference,
        description=t.description,
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

    # is_reconciled above was decided using the paise-scale bank_amount/variance --
    # this is the boundary: paise_to_rupees() applied only now, at response
    # construction, never before. confidence_score is a heuristic weight, not
    # money, and is deliberately left unconverted.
    return BatchSummary(
        id=batch.id,
        settlement_date=batch.settlement_date,
        total_gross=paise_to_rupees(batch.total_gross),
        total_refunds=paise_to_rupees(batch.total_refunds),
        total_fees=paise_to_rupees(batch.total_fees),
        total_tax=paise_to_rupees(batch.total_tax),
        total_net=paise_to_rupees(batch.total_net),
        matched_bank_amount=paise_to_rupees(bank_amount),
        match_type=match_row.match_type if match_row else None,
        confidence_score=match_row.confidence_score if match_row else None,
        match_basis=_match_basis(match_row.match_type if match_row else None),
        is_reconciled=is_reconciled,
        variance=paise_to_rupees(variance),
    )


def _get_batch_or_404(db: Session, batch_id: int) -> models.SettlementBatch:
    batch = db.query(models.SettlementBatch).filter(models.SettlementBatch.id == batch_id).first()
    if batch is None:
        raise HTTPException(status_code=404, detail=f"SettlementBatch {batch_id} not found")
    return batch


def _get_exception_or_404(db: Session, batch_id: int, exception_id: int) -> models.ExceptionRecord:
    exc = (
        db.query(models.ExceptionRecord)
        .filter(models.ExceptionRecord.id == exception_id, models.ExceptionRecord.batch_id == batch_id)
        .first()
    )
    if exc is None:
        raise HTTPException(
            status_code=404, detail=f"ExceptionRecord {exception_id} not found on batch {batch_id}"
        )
    return exc


# ---------- routes ----------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/approvers", response_model=List[DemoApproverOut])
def list_approvers():
    """The fixed list POST /exceptions/{id}/approve validates body.approver against
    (see DEMO_APPROVERS). The frontend's dropdown is populated from this endpoint
    rather than keeping its own hardcoded copy, so the two can't drift apart."""
    return [DemoApproverOut(name=name, role=role) for name, role in DEMO_APPROVERS.items()]


@app.get("/data-sources", response_model=DataSourcesResponse)
def get_data_sources(db: Session = Depends(get_db)):
    """The three conceptually independent inputs this system reconciles -- record
    counts are queried live, not hardcoded, so this stays accurate across regens."""
    return DataSourcesResponse(
        sources=[
            DataSourceOut(
                name="Razorpay Settlement Report",
                format="CSV export",
                record_count=(
                    db.query(models.SettlementBatch).count()
                    + db.query(models.SettlementEntry).count()
                ),
                description=(
                    "Settlement batches and per-order breakdown, as exported from "
                    "the Razorpay Dashboard"
                ),
            ),
            DataSourceOut(
                name="Bank Statement",
                format="CSV",
                record_count=db.query(models.BankTransaction).count(),
                description=(
                    "HDFC Bank account statement showing settlement credits and "
                    "other transactions"
                ),
            ),
            DataSourceOut(
                name="Merchant Order Records",
                format="CSV",
                record_count=db.query(models.OrderRecord).count(),
                description=(
                    "Internal order management system records, including refund "
                    "and fee data as tracked by the merchant"
                ),
            ),
        ],
        note=(
            "Synthetic data generated to match real Razorpay settlement report and "
            "bank statement formats — not a live API integration."
        ),
    )


@app.get("/bank-transactions", response_model=List[BankStatementRow])
def list_bank_transactions(db: Session = Depends(get_db)):
    """Raw bank statement lines for this period -- settlement credits and the
    unmatched noise rows alike. matched_batch_id is the existing matcher result
    (SettlementBatch.bank_transaction_id reverse), not a new decision. Narration
    verification is a separate on-demand call; this list does not run it."""
    rows = (
        db.query(models.BankTransaction)
        .order_by(models.BankTransaction.date, models.BankTransaction.id)
        .all()
    )
    return [
        BankStatementRow(
            id=row.id,
            amount=paise_to_rupees(row.amount),
            date=row.date,
            reference=row.reference,
            description=row.description,
            matched_batch_id=row.matched_batch.id if row.matched_batch else None,
        )
        for row in rows
    ]


@app.get("/bank-transactions/{bank_transaction_id}/verify-narration", response_model=NarrationVerificationOut)
def verify_bank_transaction_narration(bank_transaction_id: int, db: Session = Depends(get_db)):
    """AI verification is a read-only, on-demand check -- nothing here is cached or
    persisted, and it never feeds back into matching/bridge/exception logic. See
    app/narration_verification.py: the AI's YES/NO is cross-checked against a
    deterministic keyword rule and discarded on disagreement, same as every other
    AI feature in this codebase."""
    txn = db.query(models.BankTransaction).filter(models.BankTransaction.id == bank_transaction_id).first()
    if txn is None:
        raise HTTPException(status_code=404, detail=f"BankTransaction {bank_transaction_id} not found")

    # This dict becomes AI-prompt context -- paise_to_rupees() applied here, at the
    # boundary, so the AI never sees a 100x-inflated paise-as-rupees figure, even
    # though the current system prompt doesn't ask it to reason about amount at all.
    result = verify_narration(
        {
            "description": txn.description,
            "amount": paise_to_rupees(txn.amount),
            "date": txn.date.isoformat(),
        }
    )

    return NarrationVerificationOut(
        bank_transaction_id=txn.id,
        is_settlement_credit=result.is_settlement_credit,
        confidence_note=result.confidence_note,
        source=result.source,
    )


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
    return BatchDetail(
        **summary.model_dump(),
        entries=[_settlement_entry_out(e) for e in entries],
        bank_transaction=_bank_transaction_out(batch.bank_transaction) if batch.bank_transaction else None,
    )


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
                unexplained_amount=paise_to_rupees(e.unexplained_amount),
                suggested_action=e.suggested_action,
                status=e.status,
                requires_approval=info.get("requires_approval", False),
                severity=e.severity,
                linked_evidence_ids=_list_evidence_ids(e.linked_evidence_ids),
                approver=log.approver if log else None,
                reason=log.reason if log else None,
            )
        )
    return out


def _list_evidence_ids(linked_evidence_ids_json: Optional[str]) -> list:
    """ExceptionOut.linked_evidence_ids is a list of {type, id} refs. Anomaly
    findings store a comparison dict instead -- return [] so the queue can
    still serialize. The comparison is read from the per-exception evidence
    endpoint, not from this list field."""
    if not linked_evidence_ids_json:
        return []
    parsed = json.loads(linked_evidence_ids_json)
    return parsed if isinstance(parsed, list) else []


def _extract_evidence_ids(linked_evidence_ids_json: Optional[str]):
    """Parses one ExceptionRecord's linked_evidence_ids JSON into typed id sets.
    SYSTEMIC_FEE_DRIFT/SYSTEMIC_REFUND_DRIFT store a comparison dict here instead
    of a list of {type, id} entries (see AnomalyEvidenceOut) -- not a list, so
    there's nothing to resolve into rows; skip rather than crash on it."""
    entry_ids, order_ids, bank_ids = set(), set(), set()
    parsed = json.loads(linked_evidence_ids_json) if linked_evidence_ids_json else []
    if not isinstance(parsed, list):
        return entry_ids, order_ids, bank_ids
    for item in parsed:
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
    return EvidenceOut(
        settlement_entries=[_settlement_entry_out(e) for e in entries],
        order_records=[_order_record_out(o) for o in orders],
        bank_transactions=[_bank_transaction_out(t) for t in bank_txns],
    )


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


@app.get(
    "/batches/{batch_id}/exceptions/{exception_id}/evidence",
    response_model=Union[AnomalyEvidenceOut, EvidenceOut],
)
def get_exception_evidence(batch_id: int, exception_id: int, db: Session = Depends(get_db)):
    """Scoped to this ONE exception's own linked_evidence_ids only -- unlike
    GET /batches/{batch_id}/evidence, which aggregates across every exception on
    the batch. Same scoping principle already used by /explain.

    SYSTEMIC_FEE_DRIFT/SYSTEMIC_REFUND_DRIFT are a special case: the finding is
    an aggregate statistical comparison, not any single row, so returning the
    batch's raw SettlementEntry rows here would misrepresent what was actually
    detected. linked_evidence_ids already holds that comparison as a dict (see
    app.anomaly_detection.run_anomaly_detection) -- return it directly instead
    of resolving it as {type, id} references."""
    _get_batch_or_404(db, batch_id)
    exc = _get_exception_or_404(db, batch_id, exception_id)

    if exc.classification in ANOMALY_CLASSIFICATIONS:
        return AnomalyEvidenceOut(**json.loads(exc.linked_evidence_ids))

    entry_ids, order_ids, bank_ids = _extract_evidence_ids(exc.linked_evidence_ids)
    return _resolve_evidence(db, entry_ids, order_ids, bank_ids)


@app.post("/exceptions/{exception_id}/approve", response_model=ApprovalResponse)
def approve_exception(exception_id: int, body: ApprovalRequest, db: Session = Depends(get_db)):
    """SIMULATED OPERATOR IDENTITY: body.approver must be one of the fixed names in
    DEMO_APPROVERS (validated below) -- there is still no login/session/token behind
    it, so this only proves the caller picked a name off a list, not who they actually
    are. It's recorded verbatim into ApprovalLog and AuditEvent as that simulated
    identity, same as the frontend's own "Simulated" approve/reject copy already
    tells the user. This is a demo-scope limitation, documented here rather than
    fixed -- see ApprovalRequest.approver's own description too.

    CONCURRENCY: the open->approved/rejected transition is a compare-and-set UPDATE
    (below), not a read-then-write -- two genuinely simultaneous requests for the
    same exception can both pass the 404/validation checks with a stale in-memory
    view, but only one UPDATE can match status='open' at the database; the other
    necessarily affects 0 rows once the first commits. This is what makes the 409
    guard correct under real concurrency, not just under sequential double-clicks."""
    exc = db.query(models.ExceptionRecord).filter(models.ExceptionRecord.id == exception_id).first()
    if exc is None:
        raise HTTPException(status_code=404, detail=f"ExceptionRecord {exception_id} not found")

    if body.approver not in DEMO_APPROVERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{body.approver}' is not a recognized demo approver. "
                f"Valid names: {', '.join(DEMO_APPROVERS)}."
            ),
        )

    reason = body.reason.strip() if body.reason else None
    if body.decision == "rejected" and not reason:
        raise HTTPException(status_code=400, detail="reason is required when decision is 'rejected'")

    # The single atomic statement that actually prevents a double-write: the WHERE
    # clause is evaluated by the database against the row as it stands AT THAT
    # MOMENT, not against exc.status read above -- rowcount is the only thing this
    # function trusts to decide whether it won the race.
    result = db.execute(
        update(models.ExceptionRecord)
        .where(models.ExceptionRecord.id == exception_id, models.ExceptionRecord.status == "open")
        .values(status=body.decision)
    )

    if result.rowcount == 0:
        db.rollback()
        # A fresh read, purely for a human-readable message -- current status could
        # differ from exc.status above if another request won the race in between.
        current = db.query(models.ExceptionRecord).filter(models.ExceptionRecord.id == exception_id).first()
        raise HTTPException(
            status_code=409,
            detail=(
                f"Exception {exception_id} is already {current.status} — "
                "cannot approve/reject an already-resolved exception."
            ),
        )

    resulting_action = f"status set to '{body.decision}'"
    approval_log = models.ApprovalLog(
        exception_id=exception_id,
        approver=body.approver,
        decision=body.decision,
        reason=reason,
        timestamp=datetime.datetime.now(),
        resulting_action=resulting_action,
    )
    db.add(approval_log)
    db.flush()

    # AuditEvent rows are append-only: never update or delete an AuditEvent once
    # written. before_state is hardcoded "open" -- guaranteed by the UPDATE's own
    # WHERE clause having just matched it, for this request specifically.
    db.add(
        models.AuditEvent(
            timestamp=datetime.datetime.now(),
            actor="human",
            action="exception_reviewed",
            before_state=json.dumps({"exception_id": exception_id, "status": "open"}),
            after_state=json.dumps(
                {
                    "exception_id": exception_id,
                    "status": body.decision,
                    "approver": body.approver,
                    "decision": body.decision,
                    "reason": reason,
                }
            ),
        )
    )

    db.commit()

    return ApprovalResponse(
        exception_id=exception_id,
        status=body.decision,
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


@app.get("/stats", response_model=StatsResponse)
def get_stats(db: Session = Depends(get_db)):
    """Rollup numbers for the current classification pass. "Reconciled automatically"
    / "requires review" reflect ExceptionRecord rows as they stand RIGHT NOW (any
    status) -- there's no accumulated history of past passes to check against, since
    classify_exceptions/run_anomaly_detection wipe and recreate their own rows on
    every run (see their docstrings). time_saved is a stated, labeled ESTIMATE built
    from TIME_SAVED_MINUTES_PER_EXCEPTION, not a measurement -- see that constant's
    comment for what it assumes and why."""
    total_batches = db.query(models.SettlementBatch).count()

    batch_ids_with_exceptions = {
        row[0] for row in db.query(models.ExceptionRecord.batch_id).distinct().all()
    }
    batches_requiring_review = len(batch_ids_with_exceptions)
    batches_reconciled_automatically = total_batches - batches_requiring_review

    total_exceptions = db.query(models.ExceptionRecord).count()
    estimated_minutes_saved = round(total_exceptions * TIME_SAVED_MINUTES_PER_EXCEPTION, 1)

    return StatsResponse(
        total_batches=total_batches,
        batches_reconciled_automatically=batches_reconciled_automatically,
        batches_requiring_review=batches_requiring_review,
        time_saved=TimeSavedEstimate(
            assumption=(
                f"Assumes ~{TIME_SAVED_MINUTES_PER_EXCEPTION:.0f} minutes of manual "
                "investigation avoided per exception the system surfaced automatically "
                "(locating the mismatched rows, cross-referencing order/bank records) -- "
                "an unverified estimate, not a measured figure."
            ),
            minutes_per_exception=TIME_SAVED_MINUTES_PER_EXCEPTION,
            total_exceptions=total_exceptions,
            estimated_minutes_saved=estimated_minutes_saved,
            estimated_hours_saved=round(estimated_minutes_saved / 60, 2),
        ),
    )


@app.get("/batches/{batch_id}/exceptions/{exception_id}/explain", response_model=ExplainResponse)
def explain_exception(batch_id: int, exception_id: int, db: Session = Depends(get_db)):
    _get_batch_or_404(db, batch_id)
    exc = _get_exception_or_404(db, batch_id, exception_id)

    if exc.ai_explanation:
        return ExplainResponse(
            exception_id=exc.id, explanation=exc.ai_explanation, source="ai_generated", cached=True
        )

    entry_ids, order_ids, bank_ids = _extract_evidence_ids(exc.linked_evidence_ids)
    evidence = _resolve_evidence(db, entry_ids, order_ids, bank_ids)

    # This dict becomes AI-prompt text (and the fallback string) -- paise_to_rupees()
    # applied here, at the boundary, so the AI never sees/states a 100x-inflated
    # paise-as-rupees figure. evidence is already converted (see _resolve_evidence).
    exception_record = {
        "classification": exc.classification,
        "unexplained_amount": paise_to_rupees(exc.unexplained_amount),
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


@app.get("/trend", response_model=List[TrendEntry])
def get_trend(db: Session = Depends(get_db)):
    """One entry per batch, ordered by settlement_date, for charting reconciliation
    rate and variance over time. Reuses _batch_summary rather than recomputing
    is_reconciled/variance -- same logic /batches uses, not a second definition."""
    batches = (
        db.query(models.SettlementBatch)
        .order_by(models.SettlementBatch.settlement_date, models.SettlementBatch.id)
        .all()
    )
    entries = []
    for batch in batches:
        summary = _batch_summary(db, batch)
        entries.append(
            TrendEntry(
                settlement_date=batch.settlement_date,
                batch_id=batch.id,
                is_reconciled=summary.is_reconciled,
                variance=summary.variance,
                total_net=paise_to_rupees(batch.total_net),
            )
        )
    return entries
