"""
Deterministic bridge calculation for each SettlementBatch.

Read-only: computes numbers from existing database rows, writes nothing.
Two distinct checks are made per batch:

  1. Internal consistency: does summing this batch's own SettlementEntry rows
     (bridge_net) agree with the batch's own declared total_net? A mismatch
     here points at a problem within the settlement file itself (e.g. a
     duplicated line item) -- it says nothing about the bank yet.

  2. External reconciliation: does the batch's declared total_net agree with
     the bank transaction matching.py linked to it? This is where a real
     money variance would show up.

No AI/LLM involvement -- every number is a sum or a subtraction over rows
already in the database.
"""
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy.orm import Session

from app import models

# Set to exact equality (0) rather than a nonzero tolerance -- we have no real
# settlement data exhibiting genuine paisa-level aggregation rounding drift to
# calibrate a production tolerance against. A real deployment processing actual
# multi-line-item settlements might need a small empirically-calibrated tolerance;
# we're not guessing one here without data, same principle as disabling unverified
# refund-rate anomaly detection.
VARIANCE_TOLERANCE = 0


@dataclass
class BridgeResult:
    batch_id: int
    bridge_net: float
    total_net: float
    matched_bank_amount: Optional[float]
    variance: Optional[float]
    is_reconciled: bool


def compute_bridge(db: Session, batch_ids: Optional[List[int]] = None) -> List[BridgeResult]:
    """batch_ids: when given, computes results for only those batches. Purely a
    filter on a read-only computation -- compute_bridge never writes, so this
    is safe by construction, unlike the batch_ids parameters added to
    matching.run_matching / exceptions.classify_exceptions for the same live-
    ingestion use case (app/razorpay_ingestion.py). Default (None) is
    unchanged: every batch, exactly as before."""
    results = []

    query = db.query(models.SettlementBatch).order_by(models.SettlementBatch.id)
    if batch_ids is not None:
        query = query.filter(models.SettlementBatch.id.in_(batch_ids))

    for batch in query.all():
        entries = db.query(models.SettlementEntry).filter(models.SettlementEntry.batch_id == batch.id).all()

        sum_gross = sum(e.gross_amount for e in entries)
        sum_fee = sum(e.fee for e in entries)
        sum_tax = sum(e.tax for e in entries)
        sum_refund = sum(e.refund for e in entries)

        # No rounding needed -- these are integer paise sums, exact by construction,
        # unlike the rupee-float sums this used to round via _round2() (removed).
        bridge_net = sum_gross - sum_refund - sum_fee - sum_tax
        total_net = batch.total_net

        matched_bank_amount = batch.bank_transaction.amount if batch.bank_transaction_id else None

        if matched_bank_amount is None:
            variance = None
            is_reconciled = False
        else:
            variance = total_net - matched_bank_amount
            is_reconciled = abs(variance) <= VARIANCE_TOLERANCE

        results.append(
            BridgeResult(
                batch_id=batch.id,
                bridge_net=bridge_net,
                total_net=total_net,
                matched_bank_amount=matched_bank_amount,
                variance=variance,
                is_reconciled=is_reconciled,
            )
        )

    return results
