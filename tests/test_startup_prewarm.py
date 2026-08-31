"""
Tests for app/startup.py's investigation pre-warming: the bounded, up-to-5-
attempt outer loop that tries to land on a non-malformed AI investigation
result at boot time, so demo visitors see a representative result on first
click instead of being exposed to Haiku's measured malformed-shape rate live
(see investigation_agent.is_malformed_shape_result's docstring and
CLAUDE.md's "AI Investigation Agent -- known limitation and demo-reliability
measure").

_prewarm_investigation_attempts is the core outer-loop logic and is tested
by mocking investigate_exception() directly -- these tests are about the
loop's control flow (stop on clean, retry on malformed, never loop past the
bound, stop early on a structural failure), not about the orchestration
investigate_exception itself already covers in
tests/test_investigation_agent_live.py.

The two wrapper functions (_prewarm_adversarial_demo_investigation,
_prewarm_hero_case_investigation) are tested by mocking
_prewarm_investigation_attempts itself, isolating "where does the result get
cached, and when is caching skipped" from the attempt loop.
"""
import datetime
import json
from unittest.mock import patch

import pytest

from app import demo_cache, models
from app.investigation_agent import InvestigationResult
from app.startup import (
    MAX_INVESTIGATION_PREWARM_ATTEMPTS,
    _prewarm_adversarial_demo_investigation,
    _prewarm_hero_case_investigation,
    _prewarm_investigation_attempts,
)


def _result(status, malformed=False, source="ai_investigated"):
    contradictions = (
        ["Investigation report malformed: 'verified_facts' was not a valid list of strings (got str)."]
        if malformed
        else []
    )
    return InvestigationResult(
        exception_id=1, investigation_status=status, hypothesis="h",
        evidence_used=[], tool_calls=[], verified_facts=[], unverified_claims=[],
        contradictions=contradictions, possible_root_cause=None,
        recommended_next_step=None, confidence_basis=None,
        requires_human_review=True, source=source,
    )


def _make_fee_drift_exception(scratch_db):
    batch = models.SettlementBatch(
        settlement_date=datetime.date(2026, 10, 22), total_gross=100000, total_refunds=0,
        total_fees=3000, total_tax=500, total_net=96500,
    )
    scratch_db.add(batch)
    scratch_db.flush()
    exc = models.ExceptionRecord(
        batch_id=batch.id, unexplained_amount=79934, classification="SYSTEMIC_FEE_DRIFT",
        suggested_action="Review fee configuration.", status="open",
    )
    scratch_db.add(exc)
    scratch_db.commit()
    return exc


@pytest.fixture(autouse=True)
def _reset_hero_case_cache():
    """demo_cache.hero_case_investigation is process-global state; make sure
    no test leaks a value into another test's run."""
    demo_cache.hero_case_investigation = None
    yield
    demo_cache.hero_case_investigation = None


# --- _prewarm_investigation_attempts (the core outer loop) -------------------

def test_stops_on_first_clean_result(scratch_db):
    clean = _result("CONTRADICTED")
    with patch("app.investigation_agent.investigate_exception") as mock_investigate:
        mock_investigate.return_value = clean
        result = _prewarm_investigation_attempts(scratch_db, 1, "test case")
    assert mock_investigate.call_count == 1
    assert result is clean


def test_retries_on_malformed_and_stops_at_first_clean(scratch_db):
    malformed = _result("HUMAN_REVIEW_REQUIRED", malformed=True)
    clean = _result("VERIFIED_EXPLANATION")
    with patch("app.investigation_agent.investigate_exception") as mock_investigate:
        mock_investigate.side_effect = [malformed, malformed, clean]
        result = _prewarm_investigation_attempts(scratch_db, 1, "test case")
    assert mock_investigate.call_count == 3
    assert result is clean


def test_accepts_final_attempt_if_all_malformed_never_loops_unboundedly(scratch_db):
    malformed = _result("HUMAN_REVIEW_REQUIRED", malformed=True)
    with patch("app.investigation_agent.investigate_exception") as mock_investigate:
        mock_investigate.return_value = malformed
        result = _prewarm_investigation_attempts(scratch_db, 1, "test case")
    assert mock_investigate.call_count == MAX_INVESTIGATION_PREWARM_ATTEMPTS == 5
    assert result is malformed


def test_stops_immediately_on_non_ai_outcome_no_retry(scratch_db):
    """A structural failure (no API key, client error, budget exhausted, ...)
    would not plausibly be fixed by retrying, so this stops on attempt 1
    rather than burning all 5 attempts."""
    fallback = _result("HUMAN_REVIEW_REQUIRED", source="fallback")
    with patch("app.investigation_agent.investigate_exception") as mock_investigate:
        mock_investigate.return_value = fallback
        result = _prewarm_investigation_attempts(scratch_db, 1, "test case")
    assert mock_investigate.call_count == 1
    assert result is None


# --- _prewarm_adversarial_demo_investigation (real DB-column cache) --------

def test_adversarial_prewarm_caches_result_on_matching_exception(scratch_db):
    exc = _make_fee_drift_exception(scratch_db)
    clean = _result("CONTRADICTED")
    with patch("app.startup._prewarm_investigation_attempts", return_value=clean) as mock_attempts:
        _prewarm_adversarial_demo_investigation(scratch_db)
    mock_attempts.assert_called_once()
    assert mock_attempts.call_args[0][1] == exc.id
    scratch_db.refresh(exc)
    cached = json.loads(exc.investigation_result)
    assert cached["investigation_status"] == "CONTRADICTED"
    assert cached["cached"] is False


def test_adversarial_prewarm_skips_when_already_cached(scratch_db):
    exc = _make_fee_drift_exception(scratch_db)
    exc.investigation_result = json.dumps({"investigation_status": "CONTRADICTED"})
    scratch_db.commit()
    with patch("app.startup._prewarm_investigation_attempts") as mock_attempts:
        _prewarm_adversarial_demo_investigation(scratch_db)
    mock_attempts.assert_not_called()


def test_adversarial_prewarm_skips_when_no_matching_exception(scratch_db):
    with patch("app.startup._prewarm_investigation_attempts") as mock_attempts:
        _prewarm_adversarial_demo_investigation(scratch_db)
    mock_attempts.assert_not_called()


def test_adversarial_prewarm_does_not_cache_when_attempts_return_none(scratch_db):
    exc = _make_fee_drift_exception(scratch_db)
    with patch("app.startup._prewarm_investigation_attempts", return_value=None):
        _prewarm_adversarial_demo_investigation(scratch_db)
    scratch_db.refresh(exc)
    assert exc.investigation_result is None


# --- _prewarm_hero_case_investigation (in-memory app.demo_cache) -----------

def test_hero_case_prewarm_caches_result(scratch_db):
    clean = _result("PARTIALLY_VERIFIED")
    with patch("app.hero_case.build_hero_case_session", return_value=(scratch_db, 1, 7, None)):
        with patch("app.startup._prewarm_investigation_attempts", return_value=clean) as mock_attempts:
            _prewarm_hero_case_investigation()
    mock_attempts.assert_called_once_with(scratch_db, 7, "hero case")
    assert demo_cache.hero_case_investigation["investigation_status"] == "PARTIALLY_VERIFIED"
    assert demo_cache.hero_case_investigation["cached"] is False


def test_hero_case_prewarm_skips_when_already_cached(scratch_db):
    demo_cache.hero_case_investigation = {"investigation_status": "PARTIALLY_VERIFIED"}
    with patch("app.hero_case.build_hero_case_session") as mock_build:
        _prewarm_hero_case_investigation()
    mock_build.assert_not_called()


def test_hero_case_prewarm_skips_when_no_exception_produced(scratch_db):
    with patch("app.hero_case.build_hero_case_session", return_value=(scratch_db, 1, None, None)):
        with patch("app.startup._prewarm_investigation_attempts") as mock_attempts:
            _prewarm_hero_case_investigation()
    mock_attempts.assert_not_called()
    assert demo_cache.hero_case_investigation is None
