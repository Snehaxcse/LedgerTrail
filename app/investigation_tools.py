"""
Phase B: the bounded, read-only tool layer for the AI Reconciliation
Investigation Agent (Phase C, not built yet). Every function here is plain,
deterministic Python -- no LLM call, no arithmetic decision-making left to an
AI, nothing invented. This is the "LedgerTrail returns authoritative evidence"
half of the spec's workflow diagram; the "AI determines what evidence it
needs" half is Phase C's tool-use loop, which will call these functions,
never reimplement them.

One exception, deliberate and pre-existing: verify_narration() below wraps
app.narration_verification.verify_narration(), which DOES call the Anthropic
API -- that's the same already-built, already-cross-verified feature from
earlier in this project, reused here almost unchanged (per the phase plan).
It does not violate "no LLM involvement yet": there is still no NEW
orchestration or tool-choosing logic anywhere in this file. Every other tool
is 100% deterministic.

Design decisions worth being explicit about:

1. Money boundary: every tool that returns a currency figure converts paise
   to rupees via app.money.paise_to_rupees() before returning -- a tool result
   an LLM will read is exactly the kind of AI-facing boundary Phase 3 already
   established the rule for (API responses). Internal DB columns and any
   paise-scale comparison happen before this boundary, never after.

2. get_order()/get_refunds() take order_ref (str), not a numeric "order_id" --
   the spec named the parameter order_id, but nothing in this schema actually
   links settlement entries to orders by numeric primary key; the real,
   already-used join key everywhere else in this codebase (matching.py,
   exceptions.py, the evidence endpoints) is OrderRecord.order_ref. An agent
   that just called get_settlement_entries() only ever has order_ref strings
   in hand, never OrderRecord.id -- a literal order_id parameter would be
   practically uncallable in the real investigation workflow. Flagged here
   rather than silently renamed without comment.

3. Evidence resolution (get_exception_evidence) duplicates, rather than
   imports, the small amount of evidence-lookup logic app/main.py's
   /batches/{id}/exceptions/{id}/evidence endpoint already has. Deliberate:
   importing from app.main would risk a circular import once Phase C's agent
   endpoint lives in app/main.py and itself imports from this module, and
   touching the working hero-flow endpoint file isn't warranted just to save
   ~20 lines of duplication. Both read the exact same linked_evidence_ids
   JSON shape and the same models, so they cannot disagree about what the
   data actually is, only duplicate the code that reads it.

4. Tool architecture redesign: every tool below is now explicitly one of two
   kinds, marked by section header -- EVIDENCE tools (retrieve authoritative
   facts: settlement entries, orders, refunds, bank records, exception
   evidence, the up-front case packet, the bridge) or VERIFICATION tools
   (establish a relationship between facts the model already has:
   amount/reference/narration checks). No tool asks the model to compute
   something the backend can compute deterministically and hand back --
   get_investigation_context is the clearest instance of this: a single
   up-front case packet (exception_type, affected_orders, settlement_id,
   expected_amount, actual_amount, relevant_refunds, relevant_bank_
   transactions, relevant_dates, known_variances) that replaces several
   separate lookups the model previously had to make and stitch together
   itself. It returns FACTS ONLY -- no "reason" or "confirmed" field, nothing
   that states a conclusion; interpreting the packet is still entirely the
   model's job, this tool only removes the bookkeeping of assembling it.
"""
import json
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app import bridge, models
from app.anomaly_detection import ANOMALY_CLASSIFICATIONS
from app.matching import AMOUNT_TOLERANCE, DATE_WINDOW_DAYS, _candidates_for_batch
from app.money import paise_to_rupees
from app.narration_verification import VerificationResult
from app.narration_verification import verify_narration as _verify_narration_ai


# ---------- shared dict builders (money boundary applied here) ----------

def _batch_dict(batch: models.SettlementBatch) -> Dict[str, Any]:
    return {
        "batch_id": batch.id,
        "settlement_date": batch.settlement_date.isoformat(),
        "total_gross": paise_to_rupees(batch.total_gross),
        "total_refunds": paise_to_rupees(batch.total_refunds),
        "total_fees": paise_to_rupees(batch.total_fees),
        "total_tax": paise_to_rupees(batch.total_tax),
        "total_net": paise_to_rupees(batch.total_net),
        "bank_transaction_id": batch.bank_transaction_id,
        "is_matched": batch.bank_transaction_id is not None,
    }


def _entry_dict(entry: models.SettlementEntry) -> Dict[str, Any]:
    return {
        "settlement_entry_id": entry.id,
        "batch_id": entry.batch_id,
        "order_ref": entry.order_ref,
        "gross_amount": paise_to_rupees(entry.gross_amount),
        "fee": paise_to_rupees(entry.fee),
        "tax": paise_to_rupees(entry.tax),
        "refund": paise_to_rupees(entry.refund),
        "net_amount": paise_to_rupees(entry.net_amount),
    }


def _order_dict(order: models.OrderRecord) -> Dict[str, Any]:
    return {
        "order_record_id": order.id,
        "order_ref": order.order_ref,
        "amount": paise_to_rupees(order.amount),
        "status": order.status,
        "refund_amount": paise_to_rupees(order.refund_amount),
        "fee_amount": paise_to_rupees(order.fee_amount),
    }


def _bank_txn_dict(txn: models.BankTransaction) -> Dict[str, Any]:
    return {
        "bank_transaction_id": txn.id,
        "amount": paise_to_rupees(txn.amount),
        "date": txn.date.isoformat(),
        "reference": txn.reference,
        "description": txn.description,
    }


# ---------- evidence tools: retrieve authoritative facts -------------------
# Every tool below this line returns data the backend already has or already
# computed -- settlement/order/bank records, the exception's own linked
# evidence, the up-front case packet, the gross->net bridge. None of them ask
# the model to compute anything; if a number can be derived deterministically
# (a total, a rate, a bridge variance), the TOOL derives it and hands back the
# answer. See "verification tools" below for the other half of this split.

def get_settlement_batch(db: Session, batch_id: int) -> Optional[Dict[str, Any]]:
    batch = db.query(models.SettlementBatch).filter(models.SettlementBatch.id == batch_id).first()
    return _batch_dict(batch) if batch else None


def get_settlement_entries(db: Session, batch_id: int) -> List[Dict[str, Any]]:
    """Reuses exceptions.py's own entry lookup rather than re-querying by hand."""
    from app.exceptions import _entries_for_batch  # local import: avoids a module-load-order issue

    return [_entry_dict(e) for e in _entries_for_batch(db, batch_id)]


def get_bank_candidates(db: Session, batch_id: int) -> Dict[str, Any]:
    """Reuses matching.py's own candidate-selection function unmodified --
    the exact same amount/date logic run_matching() uses to decide what "a
    plausible match" means, not a reimplementation of it. Shows every
    plausible candidate, best first, plus which one (if any) was actually
    chosen -- useful for investigating both "why did this match the way it
    did" and "was there ever another explanation on the table"."""
    batch = db.query(models.SettlementBatch).filter(models.SettlementBatch.id == batch_id).first()
    if batch is None:
        return None
    bank_transactions = db.query(models.BankTransaction).all()
    candidates = _candidates_for_batch(batch, bank_transactions, AMOUNT_TOLERANCE, DATE_WINDOW_DAYS)
    return {
        "batch_id": batch_id,
        "amount_tolerance_rupees": paise_to_rupees(AMOUNT_TOLERANCE),
        "date_window_days": DATE_WINDOW_DAYS,
        "candidates": [
            {
                **_bank_txn_dict(txn),
                "confidence_score": confidence_score,
                "match_type": match_type,
                "date_diff_days": date_diff,
                "is_current_match": batch.bank_transaction_id == txn.id,
            }
            for txn, confidence_score, match_type, date_diff in candidates
        ],
    }


def get_order(db: Session, order_ref: str) -> Optional[Dict[str, Any]]:
    order = db.query(models.OrderRecord).filter(models.OrderRecord.order_ref == order_ref).first()
    return _order_dict(order) if order else None


def get_refunds(db: Session, order_ref: str) -> Dict[str, Any]:
    """Surfaces BOTH sides of a refund comparison without computing anything
    itself -- the order record's own refund_amount, and every settlement
    entry referencing this order_ref (there can be more than one, e.g. a
    duplicate-entry case), each with its own refund figure. Whether these
    agree is left to verify_amount_relationship() or a human, not decided here."""
    order = db.query(models.OrderRecord).filter(models.OrderRecord.order_ref == order_ref).first()
    entries = db.query(models.SettlementEntry).filter(models.SettlementEntry.order_ref == order_ref).all()
    return {
        "order_ref": order_ref,
        "order_record_refund": paise_to_rupees(order.refund_amount) if order else None,
        "settlement_entry_refunds": [
            {"settlement_entry_id": e.id, "batch_id": e.batch_id, "refund": paise_to_rupees(e.refund)}
            for e in entries
        ],
    }


def get_exception_evidence(db: Session, exception_id: int) -> Optional[Dict[str, Any]]:
    exc = db.query(models.ExceptionRecord).filter(models.ExceptionRecord.id == exception_id).first()
    if exc is None:
        return None

    if exc.classification in ANOMALY_CLASSIFICATIONS:
        # Aggregate statistical finding -- the evidence IS the comparison
        # itself (fee_rate/refund_rate ratios), not resolved rows. Ratios are
        # not currency; no paise_to_rupees() conversion applies to `comparison`.
        # unexplained_amount IS currency (converted) -- included here too, same
        # as the record_references branch below, so an investigation of an
        # anomaly classification has this authoritative figure available as
        # grounded evidence rather than only being derivable by the agent
        # doing its own rate x gross arithmetic (which the verifier correctly
        # never trusts, by design -- see app/investigation_agent.py).
        return {
            "exception_id": exc.id,
            "classification": exc.classification,
            "evidence_type": "anomaly_comparison",
            "unexplained_amount": paise_to_rupees(exc.unexplained_amount),
            "comparison": json.loads(exc.linked_evidence_ids) if exc.linked_evidence_ids else {},
        }

    entry_ids, order_ids, bank_ids = set(), set(), set()
    parsed = json.loads(exc.linked_evidence_ids) if exc.linked_evidence_ids else []
    if isinstance(parsed, list):
        for item in parsed:
            if item["type"] == "settlement_entry":
                entry_ids.add(item["id"])
            elif item["type"] == "order_record":
                order_ids.add(item["id"])
            elif item["type"] == "bank_transaction":
                bank_ids.add(item["id"])

    entries = db.query(models.SettlementEntry).filter(models.SettlementEntry.id.in_(entry_ids)).all() if entry_ids else []
    orders = db.query(models.OrderRecord).filter(models.OrderRecord.id.in_(order_ids)).all() if order_ids else []
    bank_txns = (
        db.query(models.BankTransaction).filter(models.BankTransaction.id.in_(bank_ids)).all() if bank_ids else []
    )

    return {
        "exception_id": exc.id,
        "classification": exc.classification,
        "evidence_type": "record_references",
        "unexplained_amount": paise_to_rupees(exc.unexplained_amount),
        "settlement_entries": [_entry_dict(e) for e in entries],
        "order_records": [_order_dict(o) for o in orders],
        "bank_transactions": [_bank_txn_dict(t) for t in bank_txns],
    }


# Which single currency figure this classification actually turns on, as an
# (order_field, entry_field) pair -- both read directly off the order/entry
# records, never derived. A deterministic lookup table, not model judgment:
# picking WHICH field is relevant to a classification is backend knowledge
# (exceptions.py already encodes this in its own comparisons), not something
# the model should have to infer from the classification name. Classifications
# with no single order-level amount at stake (batch-wide or date-based) are
# deliberately absent -- their evidence lives in relevant_dates/known_variances
# instead of being force-fit into a currency pair that doesn't apply to them.
_EXPECTED_ACTUAL_FIELD_BY_CLASSIFICATION = {
    "MISSING_REFUND_RECORD": ("refund_amount", "refund"),
    "REFUND_NOT_IN_SETTLEMENT": ("refund_amount", "refund"),
    "FEE_TIER_MISMATCH": ("fee_amount", "fee"),
}


def get_investigation_context(db: Session, exception_id: int) -> Optional[Dict[str, Any]]:
    """A deterministic case packet, meant as the FIRST tool call of an
    investigation -- replaces several separate lookups (get_exception_evidence,
    get_refunds per order, get_settlement_batch, calculate_bridge) the model
    previously had to make and stitch together itself before it had enough
    context to even decide what to look at next.

    Returns FACTS ONLY: raw figures, dates, and record references. No field
    here states a conclusion, a reason, or whether anything is "confirmed" --
    exception_type is included so the packet is self-describing, but nothing
    says why it's an exception or what to do about it. That interpretation is
    still entirely the model's job; this tool only removes the bookkeeping of
    gathering the pieces.

    expected_amount/actual_amount: see _EXPECTED_ACTUAL_FIELD_BY_CLASSIFICATION
    -- null for classifications with no single order-level amount at stake
    (DUPLICATE_ENTRY, TIMING_DIFFERENCE, UNMATCHED_BATCH, UNEXPLAINED_VARIANCE,
    the two SYSTEMIC_* anomaly types); their real evidence is in relevant_dates
    or known_variances instead."""
    exc = db.query(models.ExceptionRecord).filter(models.ExceptionRecord.id == exception_id).first()
    if exc is None:
        return None

    evidence = get_exception_evidence(db, exception_id)
    batch = db.query(models.SettlementBatch).filter(models.SettlementBatch.id == exc.batch_id).first()

    if evidence and evidence.get("evidence_type") == "record_references":
        affected_orders = sorted({o["order_ref"] for o in evidence["order_records"]}
                                  | {e["order_ref"] for e in evidence["settlement_entries"]})
    else:
        # Anomaly classifications (SYSTEMIC_FEE_DRIFT/SYSTEMIC_REFUND_DRIFT):
        # batch-wide, not attributable to any single order -- every order_ref
        # in the batch is "affected" in the sense of contributing to the
        # aggregate rate, listed as such rather than naming none at all.
        affected_orders = sorted({e["order_ref"] for e in get_settlement_entries(db, exc.batch_id)})

    expected_amount = actual_amount = None
    fields = _EXPECTED_ACTUAL_FIELD_BY_CLASSIFICATION.get(exc.classification)
    if fields and evidence and evidence.get("evidence_type") == "record_references":
        order_field, entry_field = fields
        order_total = sum(
            (o[order_field] for o in evidence["order_records"] if o[order_field] is not None), 0.0
        )
        entry_total = sum(e[entry_field] for e in evidence["settlement_entries"])
        expected_amount = round(order_total, 2)
        actual_amount = round(entry_total, 2)

    relevant_refunds = [get_refunds(db, order_ref) for order_ref in affected_orders]

    relevant_bank_transactions = []
    if batch and batch.bank_transaction_id:
        relevant_bank_transactions.append(_bank_txn_dict(batch.bank_transaction))

    relevant_dates = {
        "settlement_date": batch.settlement_date.isoformat() if batch else None,
        "matched_bank_transaction_date": (
            batch.bank_transaction.date.isoformat() if batch and batch.bank_transaction_id else None
        ),
    }

    bridge_result = calculate_bridge(db, exc.batch_id)
    known_variances = {
        "unexplained_amount": paise_to_rupees(exc.unexplained_amount),
        "bridge_variance": bridge_result["variance"] if bridge_result else None,
    }

    return {
        "exception_id": exc.id,
        "exception_type": exc.classification,
        "affected_orders": affected_orders,
        "settlement_id": exc.batch_id,
        "expected_amount": expected_amount,
        "actual_amount": actual_amount,
        "relevant_refunds": relevant_refunds,
        "relevant_bank_transactions": relevant_bank_transactions,
        "relevant_dates": relevant_dates,
        "known_variances": known_variances,
    }


def calculate_bridge(db: Session, batch_id: int) -> Optional[Dict[str, Any]]:
    """Reuses bridge.compute_bridge() as-is -- it computes every batch in one
    pass, so this just filters to the one requested. Not a reimplementation:
    the exact same internal-consistency and external-reconciliation numbers
    every other part of this app relies on.

    NOTE on is_reconciled: this is bridge.py's own narrower definition --
    "does the declared total_net match the bank credit amount" -- NOT the
    same thing as a batch's dashboard-level is_reconciled (app/main.py's
    _batch_summary), which also requires every open exception on the batch to
    be either resolved or not blocking. A batch can have is_reconciled=True
    here while still having an open, unresolved exception (e.g. batch 2: the
    bridge amounts match exactly, but its Fee Tier Mismatch is still open) --
    this field answers "do the numbers agree", not "is this batch fully
    clear". The investigation agent and its verifier must not conflate the two."""
    for result in bridge.compute_bridge(db):
        if result.batch_id == batch_id:
            return {
                "batch_id": result.batch_id,
                "bridge_net": paise_to_rupees(result.bridge_net),
                "total_net": paise_to_rupees(result.total_net),
                "matched_bank_amount": paise_to_rupees(result.matched_bank_amount),
                "variance": paise_to_rupees(result.variance),
                "is_reconciled": result.is_reconciled,
            }
    return None


# ---------- verification tools: establish facts deterministically ---------
# These don't retrieve new records -- they take values the model already has
# in hand (read from an evidence tool above) and deterministically establish
# a relationship between them: does A equal B within tolerance, does one
# string appear inside another, is a narration consistent with a real
# settlement credit. The model must never compute these relationships itself
# (a subtraction, a substring check, a keyword match) when a tool exists to
# establish the same fact without any risk of arithmetic drift or invented
# evidence -- see each tool's own docstring for why, and
# investigation_agent.py's SYSTEM_PROMPT for the corresponding instruction.

def verify_amount_relationship(
    amount_a: float, amount_b: float, label_a: str, label_b: str, tolerance_rupees: float = 0.0
) -> Dict[str, Any]:
    """Generic deterministic comparator for two rupee-scale figures the agent
    (or the Phase C verifier) wants to check a claimed relationship between --
    e.g. "does this order's fee_amount match the settlement entry's fee?".
    The TOOL does the subtraction, never the AI. Uses Decimal via str(), not
    Decimal(float) directly, so a rupee float that came from
    paise_to_rupees() -- already an exact decimal value -- doesn't pick up
    binary-float noise on the way back out."""
    from decimal import Decimal

    a = Decimal(str(amount_a))
    b = Decimal(str(amount_b))
    tol = Decimal(str(tolerance_rupees))
    difference = a - b
    return {
        "label_a": label_a, "amount_a": float(a),
        "label_b": label_b, "amount_b": float(b),
        "difference": float(difference),
        "tolerance_rupees": float(tol),
        "within_tolerance": abs(difference) <= tol,
    }


def verify_reference_relationship(reference_value: str, candidate_text: str) -> Dict[str, Any]:
    """Deterministic, case-insensitive containment check -- does
    reference_value (e.g. an order_ref, a UTR) appear inside candidate_text
    (e.g. a bank narration/description)? Pure string matching, no AI, no
    fuzzy/semantic judgment -- that judgment call belongs to the agent's
    hypothesis, checked against this tool's factual finding, not decided by
    this tool itself."""
    found = bool(reference_value) and bool(candidate_text) and (
        reference_value.strip().lower() in candidate_text.strip().lower()
    )
    return {
        "reference_value": reference_value,
        "candidate_text": candidate_text,
        "found": found,
        "match_type": "case_insensitive_substring",
    }


def verify_narration(db: Session, bank_transaction_id: int) -> Optional[VerificationResult]:
    """Thin wrapper: fetches the row, builds the same {description, amount,
    date} dict app/main.py's own /bank-transactions/{id}/verify-narration
    endpoint builds, and calls the real narration_verification.verify_narration()
    unchanged. This is the one tool that makes a real Anthropic API call --
    pre-existing, already cross-verified against a deterministic keyword
    check (see app/narration_verification.py), not new orchestration logic."""
    txn = db.query(models.BankTransaction).filter(models.BankTransaction.id == bank_transaction_id).first()
    if txn is None:
        return None
    return _verify_narration_ai({
        "description": txn.description,
        "amount": paise_to_rupees(txn.amount),
        "date": txn.date.isoformat(),
    })
