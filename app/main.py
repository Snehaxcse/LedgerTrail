"""
FastAPI layer over the existing reconciliation logic. Read-only except for
POST /exceptions/{id}/approve. No matching/bridge/exception computation happens
inside a REQUEST against the REAL database -- endpoints only read rows already
written by the pipeline (and record human approval decisions), so handling an
API request never re-runs classification against production data and never
discards a prior approval. The pipeline itself DOES run once, automatically, on
app boot (see app.startup.run_startup_sequence, registered below) -- that's
what guarantees a fresh deploy always starts from the known-good demo state,
independent of anything a previous visitor did.

The one deliberate exception: GET /evaluation/held-out DOES run the real
pipeline inside a request, but against a fresh isolated in-memory database
created by app.holdout_evaluation on every call -- it never imports or touches
app.database.engine/SessionLocal, so the real ledgertrail.db is structurally
unreachable from it, not just avoided by discipline. See that module's
docstring and tests/test_holdout_evaluation.py.
"""
import asyncio
import datetime
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import auth, bridge, demo_cache, models, policy
from app.ai_explain import generate_explanation
from app.anomaly_detection import ANOMALY_CLASSIFICATIONS
from app.database import get_db, ensure_schema
from app.exceptions import CLASSIFICATION_INFO
from app.hero_case import build_hero_case_session
from app.holdout_evaluation import run_holdout_evaluation
from app.holdout_sandbox import (
    build_approval_demo_sandbox,
    create_sandbox,
    get_sandbox_session,
    run_bridge_step,
    run_classification_step,
    run_idempotency_check,
    run_matching_step,
    seed_raw_records,
)
from app.investigation_agent import investigate_exception, investigation_result_to_dict
from app.matching import match_basis as _match_basis
from app.money import paise_to_rupees
from app.narration_verification import verify_narration
from app.nl_query import answer_query
from app.razorpay_ingestion import DEMO_REPLAY_PAYLOAD, ingest_razorpay_event
from app.startup import run_investigation_prewarming, run_startup_sequence

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
async def _on_startup():
    """run_startup_sequence() is called synchronously (blocks the app from
    accepting traffic, deliberately -- it's fast, a few seconds, and the
    dashboard needs real data before it's worth serving at all). Investigation
    pre-warming is then scheduled as a fire-and-forget background task via
    run_in_threadpool (so the blocking Anthropic/DB calls run on a worker
    thread, not the event loop) + asyncio.create_task (so this handler
    returns, and the app starts accepting requests, without waiting for it).

    This split exists because awaiting run_investigation_prewarming()
    synchronously here was measured to make the ENTIRE app -- not just the
    investigation endpoints -- unreachable for however long pre-warming
    takes, which can be minutes in the worst case: a live GET /batches
    issued while pre-warming was mid-flight got no response at all until
    pre-warming finished. See CLAUDE.md's "AI Investigation Agent -- known
    limitation and demo-reliability measure" and app/startup.py's module
    docstring for the full writeup."""
    run_startup_sequence()
    asyncio.create_task(run_in_threadpool(run_investigation_prewarming))

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
    # Deterministic policy check (app/policy.py) -- never triggers a new AI
    # investigation. False (with a reason) for any exception that has no
    # cached investigation_result yet, same as showing no investigation
    # trace at all until "Investigate with AI" is clicked.
    policy_eligible: bool = False
    policy_reason: Optional[str] = None


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
    # Optional and IGNORED by the real POST /exceptions/{id}/approve endpoint --
    # actor identity there comes from the authenticated session (app/auth.py),
    # never from the request body (see approve_exception / _approve_exception_core's
    # docstrings). This field still exists, and is still required in practice, only
    # for the UNRELATED holdout-sandbox approval-race demo (holdout_sandbox_approve),
    # which has no real login and still uses the original "pick a name off
    # DEMO_APPROVERS" simulated-identity mechanism.
    approver: Optional[str] = Field(
        default=None,
        description="Used ONLY by the held-out sandbox's approval-race demo, which has "
        "no real authentication -- picking a name from DEMO_APPROVERS there proves "
        "nothing about who is actually calling it. The real approve endpoint ignores "
        "this field entirely and derives the actor from the caller's session instead."
    )
    decision: Literal["approved", "rejected"]
    reason: Optional[str] = None
    # "policy_confirmed" is the Auto-resolve path: a human still picks an approver
    # from the same dropdown as a normal approve, but is asserting the deterministic
    # policy check (app/policy.py) already found this exception eligible. NEVER
    # trusted at face value -- _approve_exception_core re-runs the eligibility check
    # itself server-side before honoring this, exactly like every other value in this
    # request. A client sending "policy_confirmed" for an ineligible exception gets a
    # 400, not a silent downgrade to "manual".
    resolution_method: Literal["manual", "policy_confirmed"] = "manual"


class ApprovalResponse(BaseModel):
    exception_id: int
    status: str
    approval_log_id: int
    reason: Optional[str] = None
    resolution_method: str = "manual"


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


class HeldOutCaseOut(BaseModel):
    batch_label: str
    case_type: str
    expected_classification: Optional[str]
    detected_classification: Optional[str]
    detected: bool
    outcome: Literal["true_positive", "false_positive", "false_negative", "true_negative", "ambiguous"]
    is_reconciled: Optional[bool]
    unsafe_auto_resolution: bool
    note: Optional[str] = None


class HeldOutMetricsOut(BaseModel):
    records_evaluated: int
    planted_exceptions: int
    detected_exceptions: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: Optional[float]
    recall: Optional[float]
    unresolved_ambiguous_cases: int
    unsafe_auto_resolutions: int
    runtime_seconds: float


class HeldOutEvaluationOut(BaseModel):
    metrics: HeldOutMetricsOut
    cases: List[HeldOutCaseOut]
    dataset_note: str


class IngestionPassOut(BaseModel):
    records_seen: int
    accepted: int
    duplicates: int


class IdempotencyCheckOut(BaseModel):
    first_ingestion: IngestionPassOut
    second_ingestion: IngestionPassOut
    idempotent: bool


class SandboxStartOut(BaseModel):
    sandbox_id: str
    records_loaded: int


class SandboxMatchOut(BaseModel):
    matched_count: int
    ambiguous_excluded: int


class SandboxBridgeOut(BaseModel):
    bridges_calculated: int


class SandboxClassifyOut(BaseModel):
    total_exceptions: int
    duplicates_detected: int
    requires_review: int


class SandboxApprovalExceptionOut(BaseModel):
    id: int
    classification: str
    unexplained_amount: float
    status: str
    batch_label: Optional[str]


class SandboxApprovalStartOut(BaseModel):
    sandbox_id: str
    exception: Optional[SandboxApprovalExceptionOut]


class SandboxApproveRequest(BaseModel):
    exception_id: int
    approver: str
    decision: Literal["approved", "rejected"]
    reason: Optional[str] = None


class TimeSavedEstimate(BaseModel):
    assumption: str
    minutes_per_exception: float
    total_exceptions: int
    estimated_minutes_saved: float
    estimated_hours_saved: float


class NeedsAttentionRow(BaseModel):
    exception_id: int
    batch_id: int
    classification: str
    unexplained_amount: float
    severity: Optional[str]
    # Days since the BATCH's settlement_date -- real business data, not a demo
    # artifact. Deliberately NOT "time since this ExceptionRecord row was
    # created": every exception is recreated fresh at boot (classify_exceptions
    # wipes and reclassifies on every regen -- see CLAUDE.md), so a row's own
    # created_at would only ever read "seconds old", which would misrepresent
    # how long the underlying settlement has actually been sitting unresolved.
    age_days: int
    suggested_action: Optional[str]


class StatsResponse(BaseModel):
    total_batches: int
    total_settlement_entries: int = Field(
        description="COUNT(SettlementEntry) across all batches -- the order-level line "
        "items the settlement report actually contains."
    )
    batches_reconciled_automatically: int = Field(
        description="Batches with zero ExceptionRecord rows in the CURRENT classification "
        "pass -- never needed a human decision at all."
    )
    batches_requiring_review: int = Field(
        description="Batches with at least one ExceptionRecord row (any status: open, "
        "approved, or rejected) in the CURRENT pass -- needed at least one human decision."
    )
    unsafe_auto_resolutions: int = Field(
        description="Open exceptions where CLASSIFICATION_INFO marks requires_approval=True "
        "AND the batch's own is_reconciled computation is nonetheless True -- i.e. a case "
        "that needed a human decision but whose batch-level status could be mistaken for "
        "fully resolved. Same 'requires_approval AND reconciled' definition "
        "app/holdout_evaluation.py uses, applied to the real primary dataset instead of "
        "planted cases; reuses _batch_summary's actual is_reconciled logic rather than a "
        "separate computation, so this stays meaningful (not a tautological always-0) if "
        "that logic ever changes."
    )
    amount_at_risk: float = Field(
        description="Sum of unexplained_amount across every OPEN exception where "
        "requires_approval=True -- rupees still unaccounted for pending a human decision."
    )
    exceptions_needing_review: int = Field(
        description="COUNT of open, requires_approval=True exceptions -- the EXCEPTION-level "
        "count, not batches_requiring_review's BATCH-level count (a batch can carry more "
        "than one exception)."
    )
    oldest_unresolved_days: Optional[int] = Field(
        description="Max age_days (see NeedsAttentionRow) among open, requires_approval=True "
        "exceptions. None if there are none."
    )
    ai_investigated_count: int = Field(
        description="COUNT of exceptions (any status) with a cached investigation_result -- "
        "how many have actually been run through the AI investigation agent, not how many "
        "COULD be. Secondary/optional metric, included because it's honestly computable "
        "from an existing column, not because a new AI call was made to produce it."
    )
    needs_attention: List[NeedsAttentionRow] = Field(
        description="Open, requires_approval=True exceptions, sorted by severity (high "
        "first), then amount descending, then age descending -- the dashboard's "
        "'what needs attention right now' table."
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


class ToolCallOut(BaseModel):
    tool: str
    input: Dict[str, Any]
    result: Any


class InvestigationOut(BaseModel):
    exception_id: int
    investigation_status: Literal[
        "VERIFIED_EXPLANATION", "PARTIALLY_VERIFIED", "INSUFFICIENT_EVIDENCE",
        "CONTRADICTED", "HUMAN_REVIEW_REQUIRED",
    ]
    hypothesis: str
    evidence_used: List[str]
    tool_calls: List[ToolCallOut]
    verified_facts: List[str]
    unverified_claims: List[str]
    contradictions: List[str]
    possible_root_cause: Optional[str]
    recommended_next_step: Optional[str]
    confidence_basis: Optional[str]
    requires_human_review: bool
    ai_self_reported_status: Optional[str]
    source: Literal["ai_investigated", "fallback"]
    cached: bool


class HeroCaseOut(BaseModel):
    batch: BatchDetail
    exceptions: List[ExceptionOut]
    investigate_exception_id: int


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


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    username: str
    role: str
    display_name: str
    job_title: str


class MeResponse(BaseModel):
    username: str
    role: str
    display_name: str
    job_title: str


@app.post("/auth/login", response_model=LoginResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    """Synthetic demo credentials only (see app/auth.py's DEMO_CREDENTIALS) --
    same 401 for "no such user" and "wrong password", so a caller can't use
    this endpoint to enumerate valid usernames."""
    user = db.query(models.DemoUser).filter(models.DemoUser.username == body.username.strip().lower()).first()
    if user is None or not auth.verify_password(body.password, user.password_hash, user.password_salt):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    token = auth.create_session(db, user)
    return LoginResponse(
        token=token, username=user.username, role=user.role,
        display_name=user.display_name, job_title=user.job_title,
    )


@app.post("/auth/logout", status_code=204)
def logout(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer "):].strip()
        db.query(models.UserSession).filter(models.UserSession.token == token).delete()
        db.commit()
    return Response(status_code=204)


@app.get("/auth/me", response_model=MeResponse)
def me(current_user: auth.AuthenticatedUser = Depends(auth.get_current_user)):
    return MeResponse(
        username=current_user.username, role=current_user.role,
        display_name=current_user.display_name, job_title=current_user.job_title,
    )


@app.get("/approvers", response_model=List[DemoApproverOut])
def list_approvers():
    """The fixed list the held-out sandbox's approval-race demo validates its own
    simulated approver picker against (see DEMO_APPROVERS and
    holdout_sandbox_approve) -- unrelated to real login now that POST
    /exceptions/{id}/approve requires an authenticated "approver"-role session
    (app/auth.py) instead of a client-supplied name. Kept for that sandbox demo's
    UI, which is populated from this endpoint rather than keeping its own
    hardcoded copy."""
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
def list_batches(db: Session = Depends(get_db), current_user: auth.AuthenticatedUser = Depends(auth.get_current_user)):
    batches = db.query(models.SettlementBatch).order_by(models.SettlementBatch.id).all()
    return [_batch_summary(db, b) for b in batches]


@app.get("/batches/{batch_id}", response_model=BatchDetail)
def get_batch(batch_id: int, db: Session = Depends(get_db), current_user: auth.AuthenticatedUser = Depends(auth.get_current_user)):
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
def get_batch_exceptions(batch_id: int, db: Session = Depends(get_db), current_user: auth.AuthenticatedUser = Depends(auth.get_current_user)):
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

    # Computed once per request, not once per row -- compute_variance_by_batch
    # reuses bridge.compute_bridge, a full scan over every batch, so calling it
    # inside the loop below would recompute it once per exception for nothing.
    variance_by_batch = policy.compute_variance_by_batch(db)

    out = []
    for e in rows:
        info = CLASSIFICATION_INFO.get(e.classification, {})
        log = latest_log_by_exception.get(e.id)
        eligibility = policy.check_auto_resolution_eligibility(e, variance_by_batch.get(e.batch_id))
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
                policy_eligible=eligibility.eligible,
                policy_reason=eligibility.reason,
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
def get_exception_evidence(
    batch_id: int, exception_id: int, db: Session = Depends(get_db),
    current_user: auth.AuthenticatedUser = Depends(auth.get_current_user),
):
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


def _approve_exception_core(
    db: Session, exception_id: int, body: ApprovalRequest, *, actor: str, validate_actor: bool = True
) -> ApprovalResponse:
    """The actual compare-and-set approval logic -- extracted so both the
    real POST /exceptions/{id}/approve endpoint AND the held-out sandbox's
    approval-race demo (app/holdout_sandbox.py) call the IDENTICAL
    mechanism. Never a reimplementation that could silently drift from what
    the real endpoint does: the demo is only meaningful proof if it's
    provably running the same code, not a lookalike.

    IDENTITY: `actor` is the ONLY source of who gets recorded in
    ApprovalLog/AuditEvent -- never body.approver directly. The two real-world
    call sites resolve it very differently, which is exactly why this
    function takes an already-resolved string rather than deciding for
    itself:
      - The real endpoint (approve_exception) resolves actor from
        current_user.display_name via app.auth's Depends(require_approver) --
        a real, server-derived, role-checked identity from a session token.
        validate_actor=False there: DEMO_APPROVERS membership is irrelevant
        once real auth has already vouched for the caller.
      - The holdout-sandbox demo (holdout_sandbox_approve) still has no real
        auth -- it's a self-contained demo of the CONCURRENCY/atomicity
        mechanism, unrelated to RBAC -- and passes actor=body.approver with
        validate_actor=True, preserving its exact original "simulated
        identity" behavior (a name picked off DEMO_APPROVERS, nothing more).

    CONCURRENCY: the open->approved/rejected transition is a compare-and-set UPDATE
    (below), not a read-then-write -- two genuinely simultaneous requests for the
    same exception can both pass the 404/validation checks with a stale in-memory
    view, but only one UPDATE can match status='open' at the database; the other
    necessarily affects 0 rows once the first commits. This is what makes the 409
    guard correct under real concurrency, not just under sequential double-clicks."""
    exc = db.query(models.ExceptionRecord).filter(models.ExceptionRecord.id == exception_id).first()
    if exc is None:
        raise HTTPException(status_code=404, detail=f"ExceptionRecord {exception_id} not found")

    if validate_actor and actor not in DEMO_APPROVERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{actor}' is not a recognized demo approver. "
                f"Valid names: {', '.join(DEMO_APPROVERS)}."
            ),
        )

    reason = body.reason.strip() if body.reason else None
    if body.decision == "rejected" and not reason:
        raise HTTPException(status_code=400, detail="reason is required when decision is 'rejected'")

    # Auto-resolve is a confirmation of an approval, never a rejection -- and NEVER
    # trusted at face value: the eligibility claim in the request is re-checked here
    # against the current row, exactly the same deterministic policy.py logic that
    # decided whether the badge/button was shown in the first place. A client (or a
    # stale UI that fetched the exception list before something changed) sending
    # resolution_method="policy_confirmed" for an exception that doesn't actually
    # qualify right now gets a 400, not a silent downgrade to a manual approval.
    if body.resolution_method == "policy_confirmed":
        if body.decision != "approved":
            raise HTTPException(
                status_code=400,
                detail="resolution_method='policy_confirmed' is only valid with decision='approved'.",
            )
        eligibility = policy.check_auto_resolution_eligibility_for_batch(db, exc)
        if not eligibility.eligible:
            raise HTTPException(
                status_code=400,
                detail=f"Exception {exception_id} is not eligible for policy-confirmed auto-resolution: {eligibility.reason}",
            )

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
        # last_log gives the 409 a concrete "who already decided this" answer
        # (e.g. "Current state: APPROVED BY SNEHA") instead of just a status
        # word -- a genuine improvement for this endpoint's own callers, not
        # something added only for the sandbox demo.
        last_log = (
            db.query(models.ApprovalLog)
            .filter(models.ApprovalLog.exception_id == exception_id)
            .order_by(models.ApprovalLog.id.desc())
            .first()
        )
        detail = (
            f"Exception {exception_id} is already {current.status} — "
            "cannot approve/reject an already-resolved exception."
        )
        if last_log is not None:
            detail += f" Current state: {current.status.upper()} BY {last_log.approver.upper()}."
        raise HTTPException(status_code=409, detail=detail)

    resulting_action = (
        f"status set to '{body.decision}' (policy-confirmed auto-resolution)"
        if body.resolution_method == "policy_confirmed"
        else f"status set to '{body.decision}'"
    )
    approval_log = models.ApprovalLog(
        exception_id=exception_id,
        approver=actor,
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
    # resolution_method is folded into this existing JSON blob rather than a new
    # ApprovalLog column -- see app/policy.py's module docstring / CLAUDE.md for
    # why: this codebase has no migration tooling, and ApprovalLog is already
    # wiped and recreated on every regen, so a new structured column would need
    # either a manual ALTER TABLE or a full regen to appear on an existing DB.
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
                    "approver": actor,
                    "decision": body.decision,
                    "reason": reason,
                    "resolution_method": body.resolution_method,
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
        resolution_method=body.resolution_method,
    )


@app.post("/exceptions/{exception_id}/approve", response_model=ApprovalResponse)
def approve_exception(
    exception_id: int,
    body: ApprovalRequest,
    db: Session = Depends(get_db),
    current_user: auth.AuthenticatedUser = Depends(auth.require_approver),
):
    """403 (not 400/401) for a logged-in analyst -- require_approver runs
    AFTER get_current_user, so an unauthenticated caller gets 401 and an
    authenticated-but-wrong-role caller gets 403, distinct failure modes.
    actor is current_user.display_name -- server-derived from the session,
    never body.approver (see ApprovalRequest.approver's own description:
    that field only still exists for the unrelated holdout-sandbox demo)."""
    return _approve_exception_core(db, exception_id, body, actor=current_user.display_name, validate_actor=False)


@app.get("/audit-trail", response_model=AuditTrailResponse)
def get_audit_trail(
    limit: int = 50, offset: int = 0, db: Session = Depends(get_db),
    current_user: auth.AuthenticatedUser = Depends(auth.get_current_user),
):
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
def get_transparency(db: Session = Depends(get_db), current_user: auth.AuthenticatedUser = Depends(auth.get_current_user)):
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


@app.get("/evaluation/held-out", response_model=HeldOutEvaluationOut)
def get_held_out_evaluation():
    """Runs app.holdout_evaluation.run_holdout_evaluation() -- the real pipeline,
    against a fresh isolated in-memory database created inside that call, never
    the real ledgertrail.db (no db: Session dependency is even injected here,
    deliberately, since this endpoint has no legitimate reason to touch
    production data). See that module's docstring for the two architectural
    findings this dataset surfaces, and tests/test_holdout_evaluation.py for
    the regression test proving production data is untouched by this call."""
    result = run_holdout_evaluation()
    return HeldOutEvaluationOut(
        metrics=HeldOutMetricsOut(**result.metrics.__dict__),
        cases=[HeldOutCaseOut(**c.__dict__) for c in result.cases],
        dataset_note=result.dataset_note,
    )


@app.post("/demo/holdout-sandbox/idempotency-check", response_model=IdempotencyCheckOut)
def holdout_idempotency_check():
    """Builds one fresh isolated in-memory database (app.holdout_sandbox,
    same isolation discipline as /evaluation/held-out -- never touches
    ledgertrail.db) and ingests the held-out dataset's 14 batches into it
    twice. The second pass's duplicate count comes from a real UNIQUE
    constraint violation on HeldOutIngestionRecord.source_event_id
    (app/models.py), caught as an actual IntegrityError -- not an
    application-code "does this exist already" check a caller could bypass
    by calling the ingestion function a different way."""
    result = run_idempotency_check()
    return IdempotencyCheckOut(
        first_ingestion=IngestionPassOut(**result["first_ingestion"].__dict__),
        second_ingestion=IngestionPassOut(**result["second_ingestion"].__dict__),
        idempotent=result["idempotent"],
    )


@app.post("/demo/holdout-sandbox/reconciliation/start", response_model=SandboxStartOut)
def holdout_sandbox_start():
    """Step 1 of the live reconciliation-progress demo: creates a fresh
    isolated sandbox and loads the held-out dataset's raw records into it --
    no matching/bridge/classification yet. Returns a sandbox_id the frontend
    passes to each subsequent step so they all operate on the SAME database,
    across separate HTTP requests (see app/holdout_sandbox.py's module
    docstring for why that needs its own registry, distinct from
    /evaluation/held-out's stateless one-call-one-database pattern)."""
    sandbox_id = create_sandbox()
    db = get_sandbox_session(sandbox_id)
    try:
        records_loaded = seed_raw_records(db, sandbox_id)
    finally:
        db.close()
    return SandboxStartOut(sandbox_id=sandbox_id, records_loaded=records_loaded)


@app.post("/demo/holdout-sandbox/reconciliation/{sandbox_id}/match", response_model=SandboxMatchOut)
def holdout_sandbox_match(sandbox_id: str):
    """Step 2: the real matching.run_matching(), unmodified, against this
    sandbox's own database."""
    db = get_sandbox_session(sandbox_id)
    if db is None:
        raise HTTPException(status_code=404, detail="Sandbox not found or expired.")
    try:
        result = run_matching_step(db, sandbox_id)
    finally:
        db.close()
    return SandboxMatchOut(**result)


@app.post("/demo/holdout-sandbox/reconciliation/{sandbox_id}/bridge", response_model=SandboxBridgeOut)
def holdout_sandbox_bridge(sandbox_id: str):
    """Step 3: the real bridge.compute_bridge(), unmodified."""
    db = get_sandbox_session(sandbox_id)
    if db is None:
        raise HTTPException(status_code=404, detail="Sandbox not found or expired.")
    try:
        result = run_bridge_step(db)
    finally:
        db.close()
    return SandboxBridgeOut(**result)


@app.post("/demo/holdout-sandbox/reconciliation/{sandbox_id}/classify", response_model=SandboxClassifyOut)
def holdout_sandbox_classify(sandbox_id: str):
    """Step 4: the real exceptions.classify_exceptions() and
    anomaly_detection.run_anomaly_detection(), unmodified. Duplicate count
    and requires-review count are both sub-facts of this one step's result,
    not separately-run computations -- see run_classification_step's
    docstring for why they're still reported as distinct checkmarks."""
    db = get_sandbox_session(sandbox_id)
    if db is None:
        raise HTTPException(status_code=404, detail="Sandbox not found or expired.")
    try:
        result = run_classification_step(db)
    finally:
        db.close()
    return SandboxClassifyOut(**result)


@app.post("/demo/holdout-sandbox/approval/start", response_model=SandboxApprovalStartOut)
def holdout_sandbox_approval_start():
    """Seeds a sandbox with the full held-out pipeline (matching -> bridge ->
    classification) and picks one throwaway exception that genuinely
    requires approval, for the duplicate-approval race demo. A real
    ExceptionRecord in an isolated database -- never a primary-dataset row,
    never ledgertrail.db."""
    result = build_approval_demo_sandbox()
    exc = result["exception"]
    return SandboxApprovalStartOut(
        sandbox_id=result["sandbox_id"],
        exception=SandboxApprovalExceptionOut(**exc) if exc else None,
    )


@app.post("/demo/holdout-sandbox/approval/{sandbox_id}/approve", response_model=ApprovalResponse)
def holdout_sandbox_approve(sandbox_id: str, body: SandboxApproveRequest):
    """Calls _approve_exception_core -- the exact same compare-and-set logic
    the real POST /exceptions/{id}/approve uses -- against this sandbox's
    database. Approving the same exception twice (as different approvers)
    produces the real 409, including the real "Current state: X BY Y" detail
    pulled from this sandbox's own ApprovalLog, not a scripted message."""
    db = get_sandbox_session(sandbox_id)
    if db is None:
        raise HTTPException(status_code=404, detail="Sandbox not found or expired.")
    try:
        approval_body = ApprovalRequest(approver=body.approver, decision=body.decision, reason=body.reason)
        return _approve_exception_core(db, body.exception_id, approval_body, actor=body.approver, validate_actor=True)
    finally:
        db.close()


@app.get("/stats", response_model=StatsResponse)
def get_stats(db: Session = Depends(get_db), current_user: auth.AuthenticatedUser = Depends(auth.get_current_user)):
    """Rollup numbers for the current classification pass. "Reconciled automatically"
    / "requires review" reflect ExceptionRecord rows as they stand RIGHT NOW (any
    status) -- there's no accumulated history of past passes to check against, since
    classify_exceptions/run_anomaly_detection wipe and recreate their own rows on
    every run (see their docstrings). time_saved is a stated, labeled ESTIMATE built
    from TIME_SAVED_MINUTES_PER_EXCEPTION, not a measurement -- see that constant's
    comment for what it assumes and why."""
    total_batches = db.query(models.SettlementBatch).count()
    total_settlement_entries = db.query(models.SettlementEntry).count()

    batch_ids_with_exceptions = {
        row[0] for row in db.query(models.ExceptionRecord.batch_id).distinct().all()
    }
    batches_requiring_review = len(batch_ids_with_exceptions)
    batches_reconciled_automatically = total_batches - batches_requiring_review

    total_exceptions = db.query(models.ExceptionRecord).count()
    estimated_minutes_saved = round(total_exceptions * TIME_SAVED_MINUTES_PER_EXCEPTION, 1)

    # Reuses _batch_summary's actual is_reconciled computation (never a separate
    # definition) -- see StatsResponse.unsafe_auto_resolutions' description.
    unsafe_auto_resolutions = 0
    open_exceptions = db.query(models.ExceptionRecord).filter(models.ExceptionRecord.status == "open").all()
    batches_by_id = {b.id: b for b in db.query(models.SettlementBatch).all()}
    today = datetime.date.today()
    amount_at_risk_paise = 0
    needs_attention_rows = []
    for exc in open_exceptions:
        if not CLASSIFICATION_INFO.get(exc.classification, {}).get("requires_approval", True):
            continue
        batch = batches_by_id.get(exc.batch_id)
        if batch is not None and _batch_summary(db, batch).is_reconciled:
            unsafe_auto_resolutions += 1

        amount_at_risk_paise += exc.unexplained_amount
        # max(0, ...): the synthetic dataset's settlement dates aren't pinned to
        # "today" -- some sort after it. A settlement that hasn't happened yet by
        # the clock's reckoning hasn't aged negatively, it just hasn't aged (0),
        # so this never displays a nonsensical "-49 days".
        age_days = max(0, (today - batch.settlement_date).days) if batch is not None else 0
        needs_attention_rows.append(NeedsAttentionRow(
            exception_id=exc.id,
            batch_id=exc.batch_id,
            classification=exc.classification,
            unexplained_amount=paise_to_rupees(exc.unexplained_amount),
            severity=exc.severity,
            age_days=age_days,
            suggested_action=exc.suggested_action,
        ))

    severity_rank = {"high": 0, "medium": 1, "low": 2, "info": 3}
    needs_attention_rows.sort(
        key=lambda r: (severity_rank.get(r.severity, 99), -r.unexplained_amount, -r.age_days)
    )

    ai_investigated_count = (
        db.query(models.ExceptionRecord)
        .filter(models.ExceptionRecord.investigation_result.isnot(None))
        .count()
    )

    return StatsResponse(
        total_batches=total_batches,
        total_settlement_entries=total_settlement_entries,
        batches_reconciled_automatically=batches_reconciled_automatically,
        batches_requiring_review=batches_requiring_review,
        unsafe_auto_resolutions=unsafe_auto_resolutions,
        amount_at_risk=paise_to_rupees(amount_at_risk_paise),
        exceptions_needing_review=len(needs_attention_rows),
        oldest_unresolved_days=max((r.age_days for r in needs_attention_rows), default=None),
        ai_investigated_count=ai_investigated_count,
        needs_attention=needs_attention_rows,
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
def explain_exception(
    batch_id: int, exception_id: int, db: Session = Depends(get_db),
    current_user: auth.AuthenticatedUser = Depends(auth.get_current_user),
):
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


def _investigation_out(result, cached: bool) -> InvestigationOut:
    """Converts app.investigation_agent.InvestigationResult (a plain dataclass)
    into the API's InvestigationOut, via the same investigation_result_to_dict
    shape app/startup.py's pre-warming uses to populate the cache this
    endpoint reads -- so both always agree on exactly what a cached
    investigation looks like. No paise_to_rupees() call here -- the agent's
    own tool layer already converts every amount at ITS boundary (see
    app/investigation_tools.py), so everything arriving here is already in
    rupees."""
    return InvestigationOut(**investigation_result_to_dict(result, cached))


@app.get(
    "/batches/{batch_id}/exceptions/{exception_id}/investigate",
    response_model=InvestigationOut,
)
def investigate_exception_endpoint(
    batch_id: int, exception_id: int, db: Session = Depends(get_db),
    current_user: auth.AuthenticatedUser = Depends(auth.get_current_user),
):
    """On-demand only -- never runs automatically, same as /explain. Real
    Anthropic tool-use investigation (app.investigation_agent), several calls
    deep, so this is genuinely slow (seconds, not instant) -- cached in the DB
    like ai_explanation, and for the same reason: only a genuine
    source="ai_investigated" result is cached; a "fallback" (budget exhausted,
    API error, malformed response) is never cached, so a transient failure
    gets retried on the next click instead of being stuck permanently."""
    _get_batch_or_404(db, batch_id)
    exc = _get_exception_or_404(db, batch_id, exception_id)

    if exc.investigation_result:
        cached = InvestigationOut(**json.loads(exc.investigation_result))
        cached.cached = True
        return cached

    result = investigate_exception(db, exception_id)
    out = _investigation_out(result, cached=False)

    if result.source == "ai_investigated":
        exc.investigation_result = out.model_dump_json()
        db.commit()

    return out


@app.get("/demo/hero-case", response_model=HeroCaseOut)
def get_hero_case_demo():
    """Context for the Phase D hero case -- batch, entries, and its exceptions
    -- so the demo page can show what's being investigated before/alongside
    the investigation itself. Same isolation as investigate_hero_case_demo
    below: a fresh isolated database per call, never the real ledgertrail.db."""
    db, batch_id, missing_refund_exception_id, timing_exception_id = build_hero_case_session()
    try:
        if missing_refund_exception_id is None:
            raise HTTPException(status_code=500, detail="hero case did not produce the expected exception")

        batch = db.query(models.SettlementBatch).filter(models.SettlementBatch.id == batch_id).first()
        summary = _batch_summary(db, batch)
        entries = (
            db.query(models.SettlementEntry)
            .filter(models.SettlementEntry.batch_id == batch_id)
            .order_by(models.SettlementEntry.id)
            .all()
        )
        batch_detail = BatchDetail(
            **summary.model_dump(),
            entries=[_settlement_entry_out(e) for e in entries],
            bank_transaction=_bank_transaction_out(batch.bank_transaction) if batch.bank_transaction else None,
        )

        exception_rows = (
            db.query(models.ExceptionRecord)
            .filter(models.ExceptionRecord.batch_id == batch_id)
            .order_by(models.ExceptionRecord.id)
            .all()
        )
        exceptions_out = [
            ExceptionOut(
                id=e.id, batch_id=e.batch_id, classification=e.classification,
                unexplained_amount=paise_to_rupees(e.unexplained_amount),
                suggested_action=e.suggested_action, status=e.status,
                requires_approval=CLASSIFICATION_INFO.get(e.classification, {}).get("requires_approval", False),
                severity=e.severity, linked_evidence_ids=_list_evidence_ids(e.linked_evidence_ids),
                approver=None, reason=None,
            )
            for e in exception_rows
        ]

        return HeroCaseOut(
            batch=batch_detail, exceptions=exceptions_out,
            investigate_exception_id=missing_refund_exception_id,
        )
    finally:
        db.close()


@app.get("/demo/hero-case/investigate", response_model=InvestigationOut)
def investigate_hero_case_demo():
    """Phase D's hero case, demoed here rather than added to the primary
    dataset (which would have meant either accepting it as an 8th case in
    /transparency's carefully-verified 7/7 ground truth, or inventing a
    ground-truth entry for a case that isn't part of that dataset's actual
    purpose -- see app/hero_case.py's docstring). Builds a fresh isolated
    in-memory database on every call (same isolation discipline as
    /evaluation/held-out): never touches the real ledgertrail.db.

    Cache is app.demo_cache.hero_case_investigation, not a local module
    global here -- app/startup.py pre-warms it at boot (see CLAUDE.md's "AI
    Investigation Agent -- known limitation and demo-reliability measure"),
    and this endpoint is only responsible for reading it and falling back to
    a live call if it's still empty (e.g. pre-warming was skipped or
    failed). This fallback path -- one live investigate_exception() call,
    cached only on a genuine ai_investigated success, never a fallback -- is
    completely unchanged from before pre-warming existed."""
    if demo_cache.hero_case_investigation is not None:
        cached = InvestigationOut(**demo_cache.hero_case_investigation)
        cached.cached = True
        return cached

    db, batch_id, missing_refund_exception_id, timing_exception_id = build_hero_case_session()
    try:
        if missing_refund_exception_id is None:
            raise HTTPException(status_code=500, detail="hero case did not produce the expected exception")
        result = investigate_exception(db, missing_refund_exception_id)
        out = _investigation_out(result, cached=False)
        if result.source == "ai_investigated":
            demo_cache.hero_case_investigation = investigation_result_to_dict(result, cached=False)
        return out
    finally:
        db.close()


class RazorpayIngestionOut(BaseModel):
    duplicate: bool
    source_event_id: str
    batch_id: int
    validated: bool
    normalized: bool
    ingested: bool
    reconciled: bool
    exceptions_created: List[str]
    message: str


@app.post("/demo/razorpay-ingestion/replay", response_model=RazorpayIngestionOut)
def replay_razorpay_settlement(db: Session = Depends(get_db)):
    """Judge-facing "Replay Razorpay settlement" action -- a REAL call into
    app.razorpay_ingestion.ingest_razorpay_event against the real
    ledgertrail.db, always with the same fixed demo payload
    (source_event_id="event_razorpay_001"). Not a live Razorpay
    integration: this is a Razorpay-compatible ingestion adapter fed a fixed
    synthetic payload, not a webhook receiver.

    First click: a genuinely new batch is created and run through the
    existing (batch-scoped) reconciliation engine -- ingested=True. Second
    (and every subsequent) click: the DB-level unique constraint on
    IngestedEvent.source_event_id rejects it -- duplicate=True, the ORIGINAL
    batch_id is returned, and zero new batch/entry/bank-transaction/exception
    rows are created. See app/razorpay_ingestion.py's module docstring for
    the full idempotency mechanism."""
    result = ingest_razorpay_event(db, DEMO_REPLAY_PAYLOAD)
    if result.duplicate:
        message = "DUPLICATE EVENT — Already processed. No duplicate financial state created."
    elif result.reconciled:
        message = "Validated, normalized, ingested, and reconciled — batch created, bank credit matched, no exceptions."
    else:
        message = (
            "Validated, normalized, ingested, and reconciled — batch created, "
            f"{len(result.exceptions_created)} exception(s) raised for human review."
        )
    return RazorpayIngestionOut(
        duplicate=result.duplicate,
        source_event_id=result.source_event_id,
        batch_id=result.batch_id,
        validated=result.validated,
        normalized=result.normalized,
        ingested=result.ingested,
        reconciled=result.reconciled,
        exceptions_created=result.exceptions_created,
        message=message,
    )


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
