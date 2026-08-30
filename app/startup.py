"""
Full pipeline + demo staging, run automatically on every app boot (see the
FastAPI startup event in app/main.py) -- so a fresh Render deploy, or any
restart, always produces the exact same known-good demo state regardless of
what a previous visitor changed (e.g. approving/rejecting a different
exception). Nothing here depends on app/main.py, to avoid a circular import
between "main.py registers the startup event" and "the startup logic".

Every step is already independently idempotent (generate/ingest wipe-and-
reload with a fixed random seed; matching/classify/anomaly_detection wipe
their own prior output and recreate it) except the final demo approval,
which is made explicitly idempotent below -- so the whole sequence is safe
to run on every single app start, not just the first one.

If anything here raises, it's allowed to propagate: a demo whose data setup
silently failed but whose API looks "up" is worse than a deploy that visibly
fails to start.
"""
import datetime
import json
import logging
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from app import anomaly_detection, bridge, exceptions, matching, models
from app.database import SessionLocal, ensure_schema

logger = logging.getLogger("ledgertrail.startup")

# The one deliberate "before" example for the demo: batch 2's missing-refund
# exception, pre-approved so a visitor immediately sees both a resolved and an
# open exception rather than 7 open items with no story.
DEMO_APPROVAL_BATCH_ID = 2
DEMO_APPROVAL_CLASSIFICATION = "MISSING_REFUND_RECORD"
DEMO_APPROVER = "Sneha"


def _run_generator_and_ingest():
    """Imports and runs the two data-setup scripts exactly as they're run
    manually (python scripts/generate_synthetic_data.py, then
    python scripts/ingest.py) -- not a reimplementation of their logic."""
    scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    import generate_synthetic_data
    import ingest

    logger.info("startup: running scripts/generate_synthetic_data.py")
    generate_synthetic_data.main()

    logger.info("startup: running scripts/ingest.py")
    ingest.main()


def _apply_demo_approval(db: Session):
    """Approves batch 2's MISSING_REFUND_RECORD as the demo's "before" example.
    Explicitly idempotent: skips (does not raise, does not duplicate) if that
    exception is already approved -- this is the one step in the whole
    sequence that isn't idempotent purely by virtue of wipe-and-recreate,
    since approval status is a deliberate exception to that pattern."""
    exc = (
        db.query(models.ExceptionRecord)
        .filter(
            models.ExceptionRecord.batch_id == DEMO_APPROVAL_BATCH_ID,
            models.ExceptionRecord.classification == DEMO_APPROVAL_CLASSIFICATION,
        )
        .first()
    )
    if exc is None:
        logger.warning(
            "demo approval skipped: no %s exception found on batch %s",
            DEMO_APPROVAL_CLASSIFICATION, DEMO_APPROVAL_BATCH_ID,
        )
        return

    if exc.status != "open":
        logger.info(
            "demo approval skipped (already %s): exception id=%s", exc.status, exc.id
        )
        return

    before_status = exc.status
    exc.status = "approved"

    db.add(
        models.ApprovalLog(
            exception_id=exc.id,
            approver=DEMO_APPROVER,
            decision="approved",
            reason=None,
            timestamp=datetime.datetime.now(),
            resulting_action="status set to 'approved'",
        )
    )
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
                    "approver": DEMO_APPROVER,
                    "decision": "approved",
                    "reason": None,
                }
            ),
        )
    )
    db.commit()
    logger.info("demo approval applied: exception id=%s approved by %s", exc.id, DEMO_APPROVER)


def run_startup_sequence():
    """generate -> ingest -> match -> bridge -> classify -> anomaly detection
    -> demo approval. Safe to call on every app boot, and safe to call
    directly for local testing (see the __main__ guard below)."""
    ensure_schema()
    _run_generator_and_ingest()

    db = SessionLocal()
    try:
        logger.info("startup: running matching")
        matching.run_matching(db)

        logger.info("startup: running bridge calculation")
        bridge.compute_bridge(db)  # read-only; mirrors scripts/run_reconciliation.py's own sequence

        logger.info("startup: running exception classification")
        exceptions.classify_exceptions(db)

        logger.info("startup: running cross-batch anomaly detection")
        anomaly_detection.run_anomaly_detection(db)

        logger.info("startup: applying demo approval")
        _apply_demo_approval(db)
    finally:
        db.close()

    logger.info("startup: pipeline complete, demo state ready")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    run_startup_sequence()
