"""
Deterministic matching of SettlementBatch rows to BankTransaction rows.

Every decision here is a plain amount/date comparison against database rows --
no AI/LLM involvement, nothing inferred or guessed. A batch matches a bank
transaction when the transaction's amount is within AMOUNT_TOLERANCE of the
batch's total_net and its date is within DATE_WINDOW_DAYS of the batch's
settlement_date.
"""
import datetime
import json
import logging
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy.orm import Session

from app import models

logger = logging.getLogger("ledgertrail.matching")

# These two values were picked for this synthetic dataset (rupee rounding noise,
# and the largest deliberately-injected settlement/bank date gap being 2 days) --
# not derived from any real payment gateway's actual behavior. A production
# version would make both configurable per data source, since a different bank
# or gateway could round differently or have a longer/shorter typical credit lag.
AMOUNT_TOLERANCE = 1.00  # rupees, absorbs rounding
DATE_WINDOW_DAYS = 3


class AmbiguousMatchError(Exception):
    """Raised when a single BankTransaction is a valid candidate for more than one SettlementBatch."""


def match_basis(match_type: Optional[str]) -> Optional[str]:
    """Deterministic, human-readable restatement of match_type -- exists only so
    confidence_score isn't mistaken for a statistical confidence interval (it's a
    fixed heuristic weight: 1.0 exact, 0.85 fuzzy). No new decision is made here.
    Single source of truth: computed once here (matching.py decides match_type in
    the first place), written into Match/AuditEvent at creation time, and read
    back everywhere else (app/main.py's API responses) rather than re-derived."""
    if match_type == "exact":
        return "exact same-day match"
    if match_type == "fuzzy":
        return "fuzzy match, date offset within tolerance"
    return None


@dataclass
class MatchResult:
    batch_id: int
    matched: bool
    bank_transaction_id: Optional[int]
    confidence_score: Optional[float]
    match_type: Optional[str]


def _candidates_for_batch(batch, bank_transactions, amount_tolerance, date_window_days):
    """Returns (txn, confidence_score, match_type, date_diff) tuples within tolerance, best first."""
    candidates = []
    for txn in bank_transactions:
        amount_diff = abs(txn.amount - batch.total_net)
        if amount_diff > amount_tolerance:
            continue
        date_diff = abs((txn.date - batch.settlement_date).days)
        if date_diff > date_window_days:
            continue

        if date_diff == 0:
            confidence_score, match_type = 1.0, "exact"
        else:
            confidence_score, match_type = 0.85, "fuzzy"

        candidates.append((txn, confidence_score, match_type, date_diff))

    candidates.sort(key=lambda c: c[3])  # smallest date_diff first
    return candidates


def _reset_existing_matches(db: Session):
    """Clears prior Match rows and batch links so this function is safely re-runnable.
    Does not touch AuditEvent -- that table is append-only, never updated or deleted."""
    db.query(models.Match).delete()
    for batch in db.query(models.SettlementBatch).all():
        batch.bank_transaction_id = None
    db.commit()


def _log_match_created(db, batch_id, bank_transaction_id, confidence_score, match_type):
    # AuditEvent rows are append-only: never update or delete an AuditEvent once written.
    # match_basis is computed once, here, at write time -- not re-derived by any reader
    # (app/main.py imports this same function for its own API responses; the frontend
    # reads the stored value rather than recomputing it).
    db.add(
        models.AuditEvent(
            timestamp=datetime.datetime.now(),
            actor="system",
            action="match_created",
            before_state=None,
            after_state=json.dumps(
                {
                    "batch_id": batch_id,
                    "bank_transaction_id": bank_transaction_id,
                    "confidence_score": confidence_score,
                    "match_type": match_type,
                    "match_basis": match_basis(match_type),
                }
            ),
        )
    )


def run_matching(
    db: Session,
    amount_tolerance: float = AMOUNT_TOLERANCE,
    date_window_days: int = DATE_WINDOW_DAYS,
) -> List[MatchResult]:
    _reset_existing_matches(db)

    batches = db.query(models.SettlementBatch).all()
    bank_transactions = db.query(models.BankTransaction).all()

    per_batch_candidates = {
        batch.id: _candidates_for_batch(batch, bank_transactions, amount_tolerance, date_window_days)
        for batch in batches
    }

    # Detect any bank transaction that is a plausible candidate for more than one batch.
    # The unique constraint on SettlementBatch.bank_transaction_id would only catch this
    # after the fact (second assignment fails); we want to catch it before assigning anything.
    txn_to_batches = {}
    for batch_id, candidates in per_batch_candidates.items():
        for txn, _, _, _ in candidates:
            txn_to_batches.setdefault(txn.id, set()).add(batch_id)

    conflicts = {txn_id: bids for txn_id, bids in txn_to_batches.items() if len(bids) > 1}
    if conflicts:
        details = "; ".join(
            f"bank_transaction_id={txn_id} is a candidate for batch_ids={sorted(bids)}"
            for txn_id, bids in conflicts.items()
        )
        raise AmbiguousMatchError(
            f"Ambiguous match: one or more bank transactions are plausible candidates for "
            f"multiple settlement batches -- resolve manually before matching. {details}"
        )

    results = []
    for batch in batches:
        candidates = per_batch_candidates[batch.id]
        if not candidates:
            logger.warning(
                "No matching bank transaction for batch_id=%s (settlement_date=%s, total_net=%s)",
                batch.id, batch.settlement_date, batch.total_net,
            )
            results.append(
                MatchResult(
                    batch_id=batch.id,
                    matched=False,
                    bank_transaction_id=None,
                    confidence_score=None,
                    match_type=None,
                )
            )
            continue

        txn, confidence_score, match_type, date_diff = candidates[0]

        match = models.Match(
            settlement_batch_id=batch.id,
            bank_transaction_id=txn.id,
            confidence_score=confidence_score,
            match_type=match_type,
        )
        db.add(match)
        batch.bank_transaction_id = txn.id
        _log_match_created(db, batch.id, txn.id, confidence_score, match_type)

        logger.info(
            "Matched batch_id=%s to bank_transaction_id=%s (%s, confidence=%s, date_diff=%s days)",
            batch.id, txn.id, match_type, confidence_score, date_diff,
        )

        results.append(
            MatchResult(
                batch_id=batch.id,
                matched=True,
                bank_transaction_id=txn.id,
                confidence_score=confidence_score,
                match_type=match_type,
            )
        )

    db.commit()
    return results
