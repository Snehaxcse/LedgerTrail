"""
Tests for investigate_exception()'s orchestration layer -- the part
tests/test_investigation_agent.py deliberately doesn't cover, since that file
stays pure/no-network by testing _verify_investigation_result() directly.
"Live" here means "exercises the real orchestration code path (client
init, the tool-call loop, the retry)", not "hits the real Anthropic API" --
the client itself is mocked so these stay fast and deterministic. A 20-run
direct tally against the real API (see the Phase E investigation into the
malformed-shape retry) is what actually characterizes live model behavior;
these tests only prove the retry wiring does what it's supposed to.
"""
import datetime
from types import SimpleNamespace
from unittest.mock import patch

from app.investigation_agent import is_malformed_shape_result, investigate_exception
from app import models


def _make_exception(scratch_db, classification="SYSTEMIC_FEE_DRIFT"):
    batch = models.SettlementBatch(
        settlement_date=datetime.date(2026, 10, 22), total_gross=100000, total_refunds=0,
        total_fees=3000, total_tax=500, total_net=96500,
    )
    scratch_db.add(batch)
    scratch_db.flush()
    exc = models.ExceptionRecord(
        batch_id=batch.id, unexplained_amount=79934, classification=classification,
        suggested_action="Review fee configuration.", status="open",
    )
    scratch_db.add(exc)
    scratch_db.commit()
    return exc


def _submit_block(input_):
    return SimpleNamespace(type="tool_use", name="submit_investigation_report", input=input_, id="toolu_1")


def _response(blocks, stop_reason="tool_use"):
    return SimpleNamespace(stop_reason=stop_reason, content=blocks)


CLEAN_REPORT = {
    "hypothesis": "ok", "verified_facts": [], "unverified_claims": [], "contradictions": [],
    "possible_root_cause": "", "recommended_next_step": "", "confidence_basis": "",
    "investigation_status": "INSUFFICIENT_EVIDENCE", "requires_human_review": True,
}

# Same shape defect _verify_investigation_result's docstring describes: a
# list-typed field arriving as a bare string instead of a JSON array.
MALFORMED_REPORT = {**CLEAN_REPORT, "verified_facts": "<items>oops</items>"}


def test_clean_first_attempt_does_not_retry(monkeypatch, scratch_db):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    exc = _make_exception(scratch_db)
    create = _MockCreate([_response([_submit_block(CLEAN_REPORT)])])
    with patch("app.investigation_agent.anthropic.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create = create
        result = investigate_exception(scratch_db, exc.id)
    assert create.call_count == 1
    assert result.source == "ai_investigated"
    assert result.investigation_status == "INSUFFICIENT_EVIDENCE"


def test_malformed_first_attempt_retries_once_and_uses_clean_retry(monkeypatch, scratch_db):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    exc = _make_exception(scratch_db)
    create = _MockCreate([
        _response([_submit_block(MALFORMED_REPORT)]),
        _response([_submit_block(CLEAN_REPORT)]),
    ])
    with patch("app.investigation_agent.anthropic.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create = create
        result = investigate_exception(scratch_db, exc.id)
    assert create.call_count == 2
    assert result.source == "ai_investigated"
    assert result.investigation_status == "INSUFFICIENT_EVIDENCE"
    assert not any("malformed" in c.lower() for c in result.contradictions)


def test_malformed_both_attempts_falls_through_to_existing_defense(monkeypatch, scratch_db):
    """No third attempt, and the safety verdict itself is unchanged -- the
    retry only changes how many times the model is asked, never what the
    verifier accepts."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    exc = _make_exception(scratch_db)
    create = _MockCreate([
        _response([_submit_block(MALFORMED_REPORT)]),
        _response([_submit_block(MALFORMED_REPORT)]),
    ])
    with patch("app.investigation_agent.anthropic.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create = create
        result = investigate_exception(scratch_db, exc.id)
    assert create.call_count == 2
    assert result.investigation_status == "HUMAN_REVIEW_REQUIRED"
    assert any("malformed" in c.lower() for c in result.contradictions)


def test_is_malformed_shape_result_only_matches_the_shape_defect():
    from app.investigation_agent import InvestigationResult

    malformed = InvestigationResult(
        exception_id=1, investigation_status="HUMAN_REVIEW_REQUIRED", hypothesis="",
        evidence_used=[], tool_calls=[], verified_facts=[], unverified_claims=[],
        contradictions=["Investigation report malformed: 'verified_facts' was not a valid list of strings (got str). Discarded rather than parsed."],
        possible_root_cause=None, recommended_next_step=None, confidence_basis=None,
        requires_human_review=True, source="ai_investigated",
    )
    assert is_malformed_shape_result(malformed) is True

    genuine_contradicted = InvestigationResult(
        exception_id=1, investigation_status="CONTRADICTED", hypothesis="",
        evidence_used=[], tool_calls=[], verified_facts=[], unverified_claims=[],
        contradictions=["claim [REJECTED BY VERIFIER: states 999, which does not match any number returned]"],
        possible_root_cause=None, recommended_next_step=None, confidence_basis=None,
        requires_human_review=True, source="ai_investigated",
    )
    assert is_malformed_shape_result(genuine_contradicted) is False

    fallback = InvestigationResult(
        exception_id=1, investigation_status="HUMAN_REVIEW_REQUIRED", hypothesis="",
        evidence_used=[], tool_calls=[], verified_facts=[], unverified_claims=[], contradictions=[],
        possible_root_cause=None, recommended_next_step="Review manually.",
        confidence_basis="Investigation did not complete: no_api_key.",
        requires_human_review=True, source="fallback",
    )
    assert is_malformed_shape_result(fallback) is False


class _MockCreate:
    """Stand-in for client.messages.create -- returns each response in order
    and counts calls, without pulling in a mocking library's call-count API
    quirks."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.call_count = 0

    def __call__(self, *args, **kwargs):
        response = self._responses[self.call_count]
        self.call_count += 1
        return response
