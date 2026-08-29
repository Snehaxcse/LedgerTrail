"""
Cross-batch statistical anomaly detection. A separate, additional pass that runs
AFTER app/exceptions.py's per-batch classification -- it does not call into or
modify _classify_batch/classify_exceptions, and touches no existing single-batch
logic. Pure Python statistics (mean/stdev via the standard library); no AI
involved anywhere.

For every batch, computes fee_rate = total_fees / total_gross and
refund_rate = total_refunds / total_gross, then compares each against a baseline
built from "genuinely clean" batches -- defined as batches with zero existing
ExceptionRecord rows (from app/exceptions.py's pass) AND not themselves a
statistical outlier relative to the other candidates. That second condition is
enforced by iterative self-consistency filtering (see _stable_baseline_ids):
a batch with zero pre-existing exceptions but a wildly outlying rate (e.g. an
injected drift no other check catches) would otherwise contaminate the
reference population used to judge every other batch. Each round removes the
single worst self-outlier (by leave-one-out deviation, across either
dimension) from the candidate pool and recomputes, until nothing left in the
pool deviates from the rest of the pool by more than STDEV_THRESHOLD. This is
a general rule, not a hardcoded batch id -- it happens to converge to
excluding batch 9 given the current dataset, but would exclude whichever
batch(es) actually behave that way for any future dataset.

severity is fixed at "medium" for every finding here, never derived from
unexplained_amount the way MISSING_REFUND_RECORD/FEE_TIER_MISMATCH are in
app/exceptions.py -- a statistical outlier is uncertain by nature, not a
confirmed error, so it never defaults to "high" regardless of magnitude.
"""
import datetime
import json
import logging
import statistics
from dataclasses import dataclass
from typing import Dict, List, Set

from sqlalchemy.orm import Session

from app import models
from app.exceptions import CLASSIFICATION_INFO

logger = logging.getLogger("ledgertrail.anomaly_detection")

STDEV_THRESHOLD = 2.0
RELATIVE_DEVIATION_THRESHOLD = 0.10  # 10%: practical-significance floor, see _is_significant
SEVERITY = "medium"

# The classifications this module ever writes -- used to scope this pass's own
# re-run wipe so it never touches app/exceptions.py's output. SYSTEMIC_REFUND_DRIFT
# stays listed here (not just SYSTEMIC_FEE_DRIFT) so a re-run still cleans up any
# stale refund-drift row left over from before detection was disabled below.
ANOMALY_CLASSIFICATIONS = ("SYSTEMIC_FEE_DRIFT", "SYSTEMIC_REFUND_DRIFT")

# refund_rate detection disabled -- insufficient baseline sample size to reliably
# distinguish real drift from natural variance; revisit with a larger clean-batch
# sample or a verified planted test case. fee_rate stays active: it has a real
# planted case (batch 9) verified against it, and its natural variance across
# genuinely clean batches is tight enough (baseline stdev ~0.0001) to trust.
DIMENSIONS = (
    ("fee_rate", "SYSTEMIC_FEE_DRIFT"),
    # ("refund_rate", "SYSTEMIC_REFUND_DRIFT"),  # disabled, see comment above
)


@dataclass
class AnomalyResult:
    batch_id: int
    classification: str  # "SYSTEMIC_FEE_DRIFT" | "SYSTEMIC_REFUND_DRIFT"
    dimension: str  # "fee_rate" | "refund_rate"
    unexplained_amount: float
    observed_rate: float
    baseline_mean: float
    baseline_stdev: float
    deviation_in_stdevs: float
    relative_deviation: float
    baseline_batch_ids: List[int]


def _rate(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _existing_exception_free_ids(db: Session) -> Set[int]:
    """Batches with zero ExceptionRecord rows (any status, any classification --
    including this module's own prior findings, so a re-run doesn't treat a
    batch this pass already flagged as if it were newly clean) at the moment
    this pass starts."""
    all_ids = {row[0] for row in db.query(models.SettlementBatch.id).all()}
    ids_with_exceptions = {row[0] for row in db.query(models.ExceptionRecord.batch_id).distinct().all()}
    return all_ids - ids_with_exceptions


def _leave_one_out_stats(rates: Dict[int, float], pool: List[int], exclude_id: int):
    """mean/stdev of `rates` for every id in `pool` except exclude_id."""
    values = [rates[bid] for bid in pool if bid != exclude_id]
    mean = statistics.mean(values)
    stdev = statistics.stdev(values) if len(values) >= 2 else 0.0
    return mean, stdev


def _is_significant(observed: float, mean: float, stdev: float):
    """A deviation only counts if it clears BOTH a statistical-significance bar
    (>2 stdevs from baseline) AND a practical-significance bar (>10% relative to
    the baseline mean). Guards against a near-zero-variance baseline (e.g.
    fee_rate, which is almost deterministic across normal batches) turning
    meaningless organic noise into a "statistically significant" flag just
    because the baseline itself has almost no natural spread to compare
    against. Returns (is_significant, deviation_stdevs, relative_deviation)."""
    if stdev == 0 or mean == 0:
        return False, 0.0, 0.0
    deviation_stdevs = abs(observed - mean) / stdev
    relative_deviation = abs(observed - mean) / mean
    is_significant = deviation_stdevs > STDEV_THRESHOLD and relative_deviation > RELATIVE_DEVIATION_THRESHOLD
    return is_significant, deviation_stdevs, relative_deviation


def _stable_baseline_ids(rates_by_dimension: Dict[str, Dict[int, float]], candidate_ids: Set[int]) -> Set[int]:
    """Iteratively removes the single worst self-outlier (leave-one-out deviation
    on either dimension, against the rest of the current pool) until every
    remaining member is within STDEV_THRESHOLD of the rest of the pool, or the
    pool is too small to compute a meaningful stdev. This is what keeps a
    batch like 9 -- zero existing exceptions, but a real fee-rate outlier --
    from silently inflating the baseline used to judge every other batch.

    Uses the same both-conditions significance test as the final flagging
    decision (_is_significant): a candidate is only excluded from the baseline
    if it would ALSO qualify as "flagged" on its own terms. Without this, a
    batch could be kicked out of the baseline under a stdev-only rule while not
    itself meeting the bar to be flagged -- an inconsistent asymmetry."""
    pool = set(candidate_ids)

    while len(pool) >= 3:  # need >=2 remaining after removing the one being checked
        worst_id = None
        worst_deviation = STDEV_THRESHOLD
        pool_list = sorted(pool)

        for bid in pool_list:
            for dim, _ in DIMENSIONS:
                mean, stdev = _leave_one_out_stats(rates_by_dimension[dim], pool_list, bid)
                is_sig, deviation, _ = _is_significant(rates_by_dimension[dim][bid], mean, stdev)
                if is_sig and deviation > worst_deviation:
                    worst_deviation = deviation
                    worst_id = bid

        if worst_id is None:
            break  # stable: nothing left in the pool is a self-outlier
        logger.info(
            "baseline pool: removing batch_id=%s as a %.2f-stdev self-outlier within the candidate pool",
            worst_id, worst_deviation,
        )
        pool.discard(worst_id)

    return pool


def detect_anomalies(db: Session) -> List[AnomalyResult]:
    """Read-only: computes findings without writing anything. Use run_anomaly_detection
    to also persist them."""
    batches = db.query(models.SettlementBatch).order_by(models.SettlementBatch.id).all()

    rates_by_dimension = {
        "fee_rate": {b.id: _rate(b.total_fees, b.total_gross) for b in batches},
        "refund_rate": {b.id: _rate(b.total_refunds, b.total_gross) for b in batches},
    }
    gross_by_id = {b.id: b.total_gross for b in batches}

    candidate_ids = _existing_exception_free_ids(db)
    baseline_ids = _stable_baseline_ids(rates_by_dimension, candidate_ids)
    logger.info("final baseline batch_ids=%s (candidates were %s)", sorted(baseline_ids), sorted(candidate_ids))

    if len(baseline_ids) < 2:
        logger.warning("fewer than 2 stable baseline batches available -- skipping anomaly detection entirely")
        return []

    results = []
    for b in batches:
        for dim, classification in DIMENSIONS:
            # A baseline member is still evaluated against the OTHER baseline
            # members (leave-itself-out); a non-member is evaluated against the
            # baseline as a whole.
            if b.id in baseline_ids:
                mean, stdev = _leave_one_out_stats(rates_by_dimension[dim], sorted(baseline_ids), b.id)
                this_baseline_ids = sorted(bid for bid in baseline_ids if bid != b.id)
            else:
                values = [rates_by_dimension[dim][bid] for bid in sorted(baseline_ids)]
                mean = statistics.mean(values)
                stdev = statistics.stdev(values)
                this_baseline_ids = sorted(baseline_ids)

            observed = rates_by_dimension[dim][b.id]
            is_sig, deviation, relative_deviation = _is_significant(observed, mean, stdev)
            if not is_sig:
                continue

            unexplained_amount = round(abs((observed - mean) * gross_by_id[b.id]), 2)

            results.append(
                AnomalyResult(
                    batch_id=b.id,
                    classification=classification,
                    dimension=dim,
                    unexplained_amount=unexplained_amount,
                    observed_rate=round(observed, 6),
                    baseline_mean=round(mean, 6),
                    baseline_stdev=round(stdev, 6),
                    deviation_in_stdevs=round(deviation, 2),
                    relative_deviation=round(relative_deviation, 4),
                    baseline_batch_ids=this_baseline_ids,
                )
            )
            logger.warning(
                "path=flagged classification=%s batch_id=%s observed=%.6f baseline_mean=%.6f "
                "baseline_stdev=%.6f deviation=%.2f_stdevs relative_deviation=%.2f%% baseline_batch_ids=%s",
                classification, b.id, observed, mean, stdev, deviation, relative_deviation * 100, this_baseline_ids,
            )

    return results


def _log_anomaly_created(db, classification, unexplained_amount, requires_approval):
    # AuditEvent rows are append-only: never update or delete an AuditEvent once written.
    db.add(
        models.AuditEvent(
            timestamp=datetime.datetime.now(),
            actor="system",
            action="exception_created",
            before_state=None,
            after_state=json.dumps(
                {
                    "classification": classification,
                    "unexplained_amount": unexplained_amount,
                    "requires_approval": requires_approval,
                }
            ),
        )
    )


def run_anomaly_detection(db: Session) -> List[AnomalyResult]:
    """Computes findings and persists them as ExceptionRecord + AuditEvent rows.
    Only wipes ExceptionRecord rows this pass itself previously created (matched
    by classification) -- app/exceptions.py's per-batch exceptions, created by an
    earlier and entirely separate pass, are never touched here."""
    db.query(models.ExceptionRecord).filter(
        models.ExceptionRecord.classification.in_(ANOMALY_CLASSIFICATIONS)
    ).delete(synchronize_session=False)
    db.commit()

    results = detect_anomalies(db)

    for result in results:
        info = CLASSIFICATION_INFO[result.classification]
        entry_ids = [
            e.id
            for e in db.query(models.SettlementEntry).filter(models.SettlementEntry.batch_id == result.batch_id).all()
        ]
        evidence = [{"type": "settlement_entry", "id": eid} for eid in entry_ids]

        exc = models.ExceptionRecord(
            batch_id=result.batch_id,
            unexplained_amount=result.unexplained_amount,
            classification=result.classification,
            suggested_action=info["suggested_action"],
            status="open",
            linked_evidence_ids=json.dumps(evidence),
            severity=SEVERITY,
        )
        db.add(exc)
        db.flush()
        _log_anomaly_created(db, result.classification, result.unexplained_amount, info["requires_approval"])

    db.commit()
    return results
