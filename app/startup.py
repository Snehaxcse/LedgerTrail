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
fails to start. This applies to run_startup_sequence() (generate/ingest/
match/bridge/classify/anomaly/demo-approval) -- it is fast (a few seconds)
and is awaited synchronously by app/main.py's FastAPI startup event, so the
app does not accept ANY traffic until it completes, which is intentional:
a dashboard with no data yet would be worse than a few seconds of
unavailability at boot.

run_investigation_prewarming() is DELIBERATELY NOT part of that: investigate
_exception() calls to a live LLM can take minutes in the worst case (see
CLAUDE.md's "AI Investigation Agent -- known limitation and demo-reliability
measure"), and awaiting that synchronously inside the FastAPI startup event
was measured to make the ENTIRE app -- the dashboard, /batches, everything,
not just the investigation endpoints -- unreachable for that whole window
(confirmed live: a GET /batches issued while pre-warming was mid-flight
returned no response at all until pre-warming finished). app/main.py's
startup handler therefore calls run_startup_sequence() synchronously, then
schedules run_investigation_prewarming() as a fire-and-forget background
task (via starlette's run_in_threadpool + asyncio.create_task) AFTER the app
has already started accepting requests. Until that background task
finishes, the investigation endpoints simply fall back to their normal
live, uncached call -- exactly the same behavior as before pre-warming
existed, just for a short window instead of permanently. This function
wraps each of its two sub-calls in its own try/except for the same
best-effort reason described at its definition below.
"""
import datetime
import json
import logging
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from app import anomaly_detection, bridge, demo_cache, exceptions, matching, models
from app.database import SessionLocal, ensure_schema

logger = logging.getLogger("ledgertrail.startup")

# The one deliberate "before" example for the demo: batch 2's missing-refund
# exception, pre-approved so a visitor immediately sees both a resolved and an
# open exception rather than 7 open items with no story.
DEMO_APPROVAL_BATCH_ID = 2
DEMO_APPROVAL_CLASSIFICATION = "MISSING_REFUND_RECORD"
DEMO_APPROVER = "Sneha"

# Haiku's tool-call output has a measured 50-75% per-attempt malformed-shape
# rate on multi-tool investigations. We pre-warm the demo cache at boot to
# ensure visitors see a representative result rather than being exposed to
# this live failure rate on first click. This is a disclosed demo-reliability
# measure, not a change to the system's actual behavior or a concealment of
# the underlying limitation. (See CLAUDE.md's "AI Investigation Agent --
# known limitation and demo-reliability measure" for the full writeup, and
# investigation_agent.is_malformed_shape_result's docstring for how the rate
# was measured and what the raw malformed output actually looks like.)

# The one exception classification used as the adversarial AI-investigation
# demo (see frontend's batch 9 exception queue) -- looked up by classification
# rather than a hardcoded batch/exception id, same reasoning as
# DEMO_APPROVAL_CLASSIFICATION above.
ADVERSARIAL_DEMO_CLASSIFICATION = "SYSTEMIC_FEE_DRIFT"

# Bounded outer loop for investigation pre-warming, distinct from and
# stacked on top of investigation_agent.investigate_exception's own single
# internal retry on malformed shape. See "AI Investigation Agent -- known
# limitation and demo-reliability measure" in CLAUDE.md and
# investigation_agent.is_malformed_shape_result's docstring for the measured
# rate this is compensating for. Never loops unboundedly: whatever the final
# (5th) attempt produces is what gets cached, malformed or not.
MAX_INVESTIGATION_PREWARM_ATTEMPTS = 5


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


def _prewarm_investigation_attempts(db: Session, exception_id: int, label: str):
    """Runs investigate_exception() up to MAX_INVESTIGATION_PREWARM_ATTEMPTS
    times -- each attempt already includes investigate_exception's own
    bounded one-time retry on malformed shape, so this is a second, outer
    layer of retrying on top of that, not a duplicate of it. Stops at the
    first non-malformed ai_investigated result. If every attempt comes back
    malformed, accepts whatever the final attempt produced rather than
    looping further -- never unbounded. Returns the InvestigationResult
    actually chosen to cache, or None if a non-AI outcome (no API key,
    client/network error, tool-call budget exhausted, ...) makes further
    attempts pointless -- retrying a structural failure would not plausibly
    change the outcome, so this stops immediately rather than burning all 5
    attempts on something a retry can't fix.

    `label` is for logging only (e.g. "adversarial demo (batch 9)",
    "hero case")."""
    from app.investigation_agent import investigate_exception, is_malformed_shape_result

    result = None
    for attempt in range(1, MAX_INVESTIGATION_PREWARM_ATTEMPTS + 1):
        result = investigate_exception(db, exception_id)
        if result.source != "ai_investigated":
            logger.warning(
                "prewarm[%s] attempt %s/%s: non-AI outcome (source=%s) -- retrying would not "
                "help, stopping early",
                label, attempt, MAX_INVESTIGATION_PREWARM_ATTEMPTS, result.source,
            )
            return None
        if not is_malformed_shape_result(result):
            logger.info(
                "prewarm[%s] succeeded on attempt %s/%s: status=%s",
                label, attempt, MAX_INVESTIGATION_PREWARM_ATTEMPTS, result.investigation_status,
            )
            return result
        logger.warning(
            "prewarm[%s] attempt %s/%s: malformed-shape outcome, retrying",
            label, attempt, MAX_INVESTIGATION_PREWARM_ATTEMPTS,
        )

    logger.warning(
        "prewarm[%s] exhausted all %s attempts without a clean result -- caching the final "
        "attempt's outcome (status=%s) rather than looping further",
        label, MAX_INVESTIGATION_PREWARM_ATTEMPTS, result.investigation_status if result else None,
    )
    return result


def _prewarm_adversarial_demo_investigation(db: Session):
    """Pre-warms the real DB-column cache (ExceptionRecord.investigation_result)
    for the adversarial AI-investigation demo case, so the exception queue's
    "Investigate with AI" button shows a representative result on first
    click instead of exposing a visitor to Haiku's measured malformed-shape
    rate live. Skips quietly (never raises) if the exception doesn't exist
    in this dataset, or if it's already cached from a previous boot/manual
    run -- pre-warming should never overwrite a result someone already saw."""
    from app.investigation_agent import investigation_result_to_dict

    exc = (
        db.query(models.ExceptionRecord)
        .filter(models.ExceptionRecord.classification == ADVERSARIAL_DEMO_CLASSIFICATION)
        .first()
    )
    if exc is None:
        logger.warning(
            "prewarm[adversarial demo]: skipped, no %s exception found", ADVERSARIAL_DEMO_CLASSIFICATION
        )
        return
    if exc.investigation_result:
        logger.info("prewarm[adversarial demo]: skipped, exception id=%s already cached", exc.id)
        return

    result = _prewarm_investigation_attempts(db, exc.id, "adversarial demo (SYSTEMIC_FEE_DRIFT)")
    if result is not None:
        exc.investigation_result = json.dumps(investigation_result_to_dict(result, cached=False))
        db.commit()


def _prewarm_hero_case_investigation():
    """Pre-warms app.demo_cache.hero_case_investigation (the hero case has no
    persistent exception row, so it can't use the DB-column cache the way
    the adversarial demo does). Builds its own isolated in-memory database
    exactly like the /demo/hero-case/investigate endpoint does, and closes
    it when done -- never touches the real ledgertrail.db. Skips quietly if
    the hero case dataset doesn't produce the expected exception, or if it's
    already cached (idempotent across restarts, same as the DB-column case)."""
    from app.hero_case import build_hero_case_session
    from app.investigation_agent import investigation_result_to_dict

    if demo_cache.hero_case_investigation is not None:
        logger.info("prewarm[hero case]: skipped, already cached")
        return

    hero_db, batch_id, missing_refund_exception_id, _timing_exception_id = build_hero_case_session()
    try:
        if missing_refund_exception_id is None:
            logger.warning("prewarm[hero case]: skipped, no missing-refund exception produced")
            return
        result = _prewarm_investigation_attempts(hero_db, missing_refund_exception_id, "hero case")
        if result is not None:
            demo_cache.hero_case_investigation = investigation_result_to_dict(result, cached=False)
    finally:
        hero_db.close()


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


def run_investigation_prewarming():
    """The two AI investigation pre-warm calls, deliberately NOT part of
    run_startup_sequence() -- see this module's docstring for why (in short:
    awaiting live LLM calls synchronously inside the FastAPI startup event
    was measured to make the entire app unreachable, not just the
    investigation endpoints, for however long pre-warming takes, which can
    be minutes in the worst case). app/main.py's startup handler calls
    run_startup_sequence() synchronously (fast, needed before the app can
    show real data) and THEN schedules this function as a fire-and-forget
    background task, after the app has already started accepting requests.

    Opens its own DB session for the adversarial-demo case (the hero case
    manages its own isolated in-memory session internally) since by the time
    this runs in the background, run_startup_sequence()'s own session has
    already been closed.

    Each of the two pre-warm calls is independently wrapped in try/except:
    this is a demo-reliability nicety, not core data setup, so one failing
    (e.g. ANTHROPIC_API_KEY not configured, or a transient Anthropic API
    error) must never prevent the other from running, and must never raise
    into the background task machinery -- it only means the affected
    "Investigate with AI" button falls back to a live, uncached call on
    first click, exactly as it did before this feature existed."""
    db = SessionLocal()
    try:
        try:
            logger.info("prewarm: starting adversarial demo investigation pre-warm")
            _prewarm_adversarial_demo_investigation(db)
        except Exception:
            logger.exception("prewarm: adversarial demo investigation pre-warm failed, continuing")

        try:
            logger.info("prewarm: starting hero case investigation pre-warm")
            _prewarm_hero_case_investigation()
        except Exception:
            logger.exception("prewarm: hero case investigation pre-warm failed, continuing")
    finally:
        db.close()

    logger.info("prewarm: investigation pre-warming complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    run_startup_sequence()
    run_investigation_prewarming()
