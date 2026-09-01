"""
Deterministic auto-resolution policy check. No AI/LLM involvement -- every
input here is either a plain column already computed at classification/
investigation time, or a subtraction already performed by bridge.py.

Eligibility rule (confirmed design, do not loosen without an explicit
decision -- see CLAUDE.md):
  1. bank variance == 0 for the exception's batch
  2. an AI investigation has been run AND every claim in it is grounded --
     no unverified_claims, no contradictions, at least one verified_fact
  3. severity is not "high"

NOTE on variance duplication: this reuses bridge.compute_bridge(), which is
a SEPARATE implementation of "total_net - bank_amount" from the one embedded
directly in app/main.py's _batch_summary (the one that actually backs
GET /batches' variance/is_reconciled, i.e. what a human approver sees on the
dashboard). Both are paise-scale and both compare against the same shared
bridge.VARIANCE_TOLERANCE constant, so they agree today -- but they are two
independent code paths computing the same fact, a pre-existing duplication
in this codebase (see bridge.py's own docstring on its narrower is_reconciled
scope for the precedent). tests/test_policy.py asserts the two stay in
agreement so any future divergence fails CI immediately instead of silently
letting the policy layer and the dashboard disagree about what "reconciled"
means.
"""
import json
from dataclasses import dataclass
from typing import Dict, Optional

from sqlalchemy.orm import Session

from app import bridge, models


@dataclass
class PolicyEligibility:
    eligible: bool
    reason: str  # always set -- either why it qualifies or the specific blocking condition


def compute_variance_by_batch(db: Session) -> Dict[int, Optional[int]]:
    """Paise-scale variance per batch, computed once and reused across every
    exception row on a batch listing -- avoids recomputing compute_bridge()
    (a full-table scan over every batch) once per exception."""
    return {result.batch_id: result.variance for result in bridge.compute_bridge(db)}


def check_auto_resolution_eligibility(
    exc: models.ExceptionRecord, batch_variance: Optional[int]
) -> PolicyEligibility:
    """Pure function -- takes the exception row and its batch's already-computed
    variance (paise int or None), returns whether it qualifies for the
    Auto-resolve action. Never triggers a new investigation and never reads
    anything from the AI-facing side of investigation_result other than the
    three already-verified list fields (verified_facts/unverified_claims/
    contradictions) -- those are populated post-verification only (see
    models.ExceptionRecord.investigation_result's own comment: "never caches
    source='fallback'"), so this cannot be tricked by unverified AI output."""
    if exc.severity == "high":
        return PolicyEligibility(False, "Severity is high -- auto-resolution is never offered for high-severity exceptions.")

    if not exc.investigation_result:
        return PolicyEligibility(False, "No AI investigation has been run for this exception yet.")

    try:
        result = json.loads(exc.investigation_result)
    except (TypeError, ValueError):
        return PolicyEligibility(False, "Cached investigation result could not be parsed.")

    if result.get("unverified_claims"):
        return PolicyEligibility(
            False, "Investigation has unverified claims -- not all evidence was confirmed against source records."
        )
    if result.get("contradictions"):
        return PolicyEligibility(False, "Investigation has unresolved contradictions.")
    if not result.get("verified_facts"):
        return PolicyEligibility(False, "Investigation has no verified facts to ground a decision on.")

    if batch_variance is None:
        return PolicyEligibility(False, "Batch is not matched to a bank transaction -- variance cannot be computed.")
    if batch_variance != 0:
        return PolicyEligibility(False, f"Batch bank variance is not zero ({batch_variance} paise).")

    return PolicyEligibility(
        True, "Bank variance is zero, every investigation claim is verified, and severity is not high."
    )


def check_auto_resolution_eligibility_for_batch(db: Session, exc: models.ExceptionRecord) -> PolicyEligibility:
    """Convenience wrapper for single-exception call sites (e.g. the approve
    endpoint's server-side re-validation) where computing the whole batch's
    variance map for one row would be wasteful ceremony. GET /batches/{id}/
    exceptions uses compute_variance_by_batch() + check_auto_resolution_eligibility()
    directly instead, to avoid recomputing compute_bridge() per row."""
    variance = compute_variance_by_batch(db).get(exc.batch_id)
    return check_auto_resolution_eligibility(exc, variance)
