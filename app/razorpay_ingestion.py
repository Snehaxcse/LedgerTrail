"""
Razorpay-shaped ingestion adapter -- a live, callable version of the pipeline
scripts/ingest.py runs offline at boot:

  Razorpay-shaped synthetic settlement payload -> schema validation ->
  source_event_id -> idempotency check -> raw event record -> normalization
  to integer paise -> the SAME existing reconciliation engine
  (matching.run_matching / bridge.compute_bridge / exceptions.classify_exceptions)
  already used everywhere else, scoped to just the newly-created batch (see
  those functions' batch_ids parameter) so a live ingestion can never
  disturb the primary dataset's existing matches, exceptions, approvals,
  investigation results, or audit trail.

NOT a live Razorpay integration -- this is a Razorpay-compatible ingestion
adapter fed synthetic/test data, wired to the real backend and the real
ledgertrail.db. No webhook signature verification is implemented here (out
of scope for this pass, and not safe to fake).

IDEMPOTENCY: rests entirely on IngestedEvent.source_event_id's DB-level
UNIQUE constraint (app/models.py), not a check-then-insert (which would
race under concurrent replays). The raw event row is flushed FIRST, before
any batch/entry/bank-transaction/exception row is created; if that flush
raises IntegrityError, this is a genuine replay of an event already
ingested -- roll back (nothing else was written) and return the ORIGINAL
batch, with zero new financial records. Same proven pattern as
HeldOutIngestionRecord (app/holdout_sandbox.py), applied to the real DB.
"""
import datetime
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import matching, exceptions as exceptions_module, models
from scripts.ingest import parse_rupees_to_paise


class RazorpaySettlementEntryPayload(BaseModel):
    order_ref: str
    gross_amount: Decimal
    fee: Decimal
    tax: Decimal
    refund: Decimal = Decimal("0")
    net_amount: Decimal


class RazorpayBankCreditPayload(BaseModel):
    amount: Decimal
    date: datetime.date
    reference: str
    description: Optional[str] = None


class RazorpaySettlementEventPayload(BaseModel):
    """The external, Razorpay-shaped shape this adapter accepts -- rupee
    decimal amounts (as a real Razorpay settlement export would report them),
    converted to integer paise inside ingest_razorpay_event, never before.
    total_gross/total_refunds/total_fees/total_tax/total_net are read as
    DECLARED values, same principle as scripts/ingest.py's settlement_batches.csv:
    never derived by summing entries, so the bridge's internal-consistency
    check (entries-sum vs declared total_net) stays a meaningful check rather
    than a tautology."""
    source_event_id: str
    settlement_date: datetime.date
    total_gross: Decimal
    total_refunds: Decimal
    total_fees: Decimal
    total_tax: Decimal
    total_net: Decimal
    entries: List[RazorpaySettlementEntryPayload] = Field(min_length=1)
    bank_credit: RazorpayBankCreditPayload


@dataclass
class IngestionResult:
    duplicate: bool
    source_event_id: str
    batch_id: int
    validated: bool
    normalized: bool
    ingested: bool
    reconciled: bool
    exceptions_created: List[str] = field(default_factory=list)


# Fixed demo payload for the judge-facing "Replay Razorpay settlement" action
# (POST /demo/razorpay-ingestion/replay) -- deliberately balanced to reconcile
# cleanly (gross - refund - fee - tax == net == bank credit amount, no
# duplicate order_ref) so the demo's first click shows a clean
# Ingested/Reconciled/Batch-created result, not an exception needing
# investigation (that story is already told elsewhere in the app).
DEMO_REPLAY_PAYLOAD = RazorpaySettlementEventPayload(
    source_event_id="event_razorpay_001",
    settlement_date=datetime.date(2026, 11, 12),
    total_gross=Decimal("25000.00"),
    total_refunds=Decimal("0.00"),
    total_fees=Decimal("625.00"),
    total_tax=Decimal("112.50"),
    total_net=Decimal("24262.50"),
    entries=[
        RazorpaySettlementEntryPayload(
            order_ref="RZP-DEMO-0001",
            gross_amount=Decimal("25000.00"),
            fee=Decimal("625.00"),
            tax=Decimal("112.50"),
            refund=Decimal("0.00"),
            net_amount=Decimal("24262.50"),
        ),
    ],
    bank_credit=RazorpayBankCreditPayload(
        amount=Decimal("24262.50"),
        date=datetime.date(2026, 11, 12),
        reference="UTR700000000001",
        # Real Razorpay settlement-credit narration format (researched, not
        # guessed -- see CLAUDE.md's Group 2 notes): no batch id embedded.
        description="NEFT CR: HDFC BANK UTR700000000001 RAZORPAY SETTLEMENT",
    ),
)


def _log_event_ingested(db: Session, source_event_id: str, batch_id: int):
    # AuditEvent rows are append-only: never update or delete one once written.
    db.add(
        models.AuditEvent(
            timestamp=datetime.datetime.now(),
            actor="system",
            action="event_ingested",
            before_state=None,
            after_state=json.dumps({"source_event_id": source_event_id, "batch_id": batch_id}),
        )
    )


def ingest_razorpay_event(db: Session, payload: RazorpaySettlementEventPayload) -> IngestionResult:
    """payload is already schema-validated by construction (Pydantic raises
    pydantic.ValidationError for a malformed dict before this function is ever
    called -- see the /demo/razorpay-ingestion/replay endpoint, which is the
    boundary that actually receives untrusted input)."""
    event = models.IngestedEvent(
        source_event_id=payload.source_event_id,
        raw_payload=payload.model_dump_json(),
        batch_id=None,
        ingested_at=datetime.datetime.now(),
    )
    db.add(event)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(models.IngestedEvent)
            .filter(models.IngestedEvent.source_event_id == payload.source_event_id)
            .first()
        )
        return IngestionResult(
            duplicate=True,
            source_event_id=payload.source_event_id,
            batch_id=existing.batch_id,
            validated=True,
            normalized=True,
            ingested=False,
            reconciled=_is_batch_reconciled(db, existing.batch_id),
            exceptions_created=[],
        )

    # Normalization: rupee Decimal -> integer paise, exact (Decimal math, no
    # float), same helper scripts/ingest.py uses for the offline CSV path.
    batch = models.SettlementBatch(
        settlement_date=payload.settlement_date,
        total_gross=parse_rupees_to_paise(payload.total_gross),
        total_refunds=parse_rupees_to_paise(payload.total_refunds),
        total_fees=parse_rupees_to_paise(payload.total_fees),
        total_tax=parse_rupees_to_paise(payload.total_tax),
        total_net=parse_rupees_to_paise(payload.total_net),
    )
    db.add(batch)
    db.flush()

    for entry in payload.entries:
        db.add(
            models.SettlementEntry(
                batch_id=batch.id,
                order_ref=entry.order_ref,
                gross_amount=parse_rupees_to_paise(entry.gross_amount),
                fee=parse_rupees_to_paise(entry.fee),
                tax=parse_rupees_to_paise(entry.tax),
                refund=parse_rupees_to_paise(entry.refund),
                net_amount=parse_rupees_to_paise(entry.net_amount),
            )
        )

    bank_txn = models.BankTransaction(
        amount=parse_rupees_to_paise(payload.bank_credit.amount),
        date=payload.bank_credit.date,
        reference=payload.bank_credit.reference,
        description=payload.bank_credit.description,
    )
    db.add(bank_txn)

    event.batch_id = batch.id
    db.commit()

    # Existing reconciliation engine, scoped to just this new batch -- see the
    # batch_ids parameter added to each of these for exactly this call site.
    # No other batch's Match/ExceptionRecord/audit state is touched.
    matching.run_matching(db, batch_ids=[batch.id])
    results = exceptions_module.classify_exceptions(db, batch_ids=[batch.id])

    _log_event_ingested(db, payload.source_event_id, batch.id)
    db.commit()

    exceptions_created = [r.classification for r in results if r.classification is not None]

    return IngestionResult(
        duplicate=False,
        source_event_id=payload.source_event_id,
        batch_id=batch.id,
        validated=True,
        normalized=True,
        ingested=True,
        reconciled=len(exceptions_created) == 0,
        exceptions_created=exceptions_created,
    )


def _is_batch_reconciled(db: Session, batch_id: Optional[int]) -> bool:
    if batch_id is None:
        return False
    return (
        db.query(models.ExceptionRecord)
        .filter(models.ExceptionRecord.batch_id == batch_id)
        .count()
        == 0
    )
