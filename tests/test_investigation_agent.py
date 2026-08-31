"""
Fast, deterministic, no-network tests for the investigation agent's verifier
(app.investigation_agent._verify_investigation_result). This is the actual
regression proof that "the verifier rejects unsupported claims" -- runs on
synthetic AI output against synthetic tool evidence, so it's free and
repeatable, never depending on model non-determinism. Live-API integration
tests (real investigate_exception() calls against real exceptions) are in
tests/test_investigation_agent_live.py, kept separate so this file stays fast.
"""
from app.investigation_agent import ToolCallRecord, _verify_investigation_result


def _log(*records):
    return [ToolCallRecord(tool=name, input=inp, result=res) for name, inp, res in records]


def test_grounded_verified_fact_survives():
    log = _log(("get_settlement_batch", {"batch_id": 2}, {"total_net": 116294.35, "batch_id": 2}))
    raw = {
        "hypothesis": "The batch's total_net is 116294.35.",
        "verified_facts": ["The batch's declared total_net is Rs.116294.35."],
        "unverified_claims": [],
        "contradictions": [],
        "possible_root_cause": "", "recommended_next_step": "", "confidence_basis": "",
        "investigation_status": "VERIFIED_EXPLANATION", "requires_human_review": False,
    }
    result = _verify_investigation_result(raw, log, classification="TIMING_DIFFERENCE")
    assert result["verified_facts"] == ["The batch's declared total_net is Rs.116294.35."]
    assert result["contradictions"] == []
    assert result["investigation_status"] == "VERIFIED_EXPLANATION"


def test_fabricated_number_in_verified_fact_is_rejected_not_passed_through():
    """THE core safety guarantee: a claim citing a number no tool ever
    returned must never survive into the final verified_facts list."""
    log = _log(("get_settlement_batch", {"batch_id": 2}, {"total_net": 116294.35, "batch_id": 2}))
    raw = {
        "hypothesis": "ok",
        "verified_facts": ["The batch's declared total_net is Rs.999999.99."],  # fabricated
        "unverified_claims": [], "contradictions": [],
        "possible_root_cause": "", "recommended_next_step": "", "confidence_basis": "",
        "investigation_status": "VERIFIED_EXPLANATION", "requires_human_review": False,
    }
    result = _verify_investigation_result(raw, log, classification="TIMING_DIFFERENCE")
    assert result["verified_facts"] == []
    assert len(result["contradictions"]) == 1
    assert "999999.99" in result["contradictions"][0]
    assert "REJECTED BY VERIFIER" in result["contradictions"][0]
    assert result["investigation_status"] == "CONTRADICTED"


def test_fabricated_number_in_narrative_text_also_caught():
    """Not just verified_facts -- a fabricated number smuggled into the
    hypothesis/root-cause/next-step prose must be caught too."""
    log = _log(("get_exception_evidence", {"exception_id": 1}, {"unexplained_amount": 5786.83}))
    raw = {
        "hypothesis": "This looks like a Rs.12345.00 discrepancy caused by a refund.",
        "verified_facts": [], "unverified_claims": [], "contradictions": [],
        "possible_root_cause": "", "recommended_next_step": "", "confidence_basis": "",
        "investigation_status": "VERIFIED_EXPLANATION", "requires_human_review": False,
    }
    result = _verify_investigation_result(raw, log, classification="MISSING_REFUND_RECORD")
    assert result["investigation_status"] == "CONTRADICTED"
    assert any("12345" in c for c in result["contradictions"])


def test_insufficient_evidence_self_report_is_respected_when_uncontradicted():
    log = _log(("get_settlement_batch", {"batch_id": 3}, {"batch_id": 3, "total_net": 100.0}))
    raw = {
        "hypothesis": "Not enough evidence to explain this.",
        "verified_facts": [], "unverified_claims": ["Possibly a timing issue, unconfirmed."],
        "contradictions": [],
        "possible_root_cause": None, "recommended_next_step": "Escalate to manual review.",
        "confidence_basis": "Available tools did not surface an explanation.",
        "investigation_status": "INSUFFICIENT_EVIDENCE", "requires_human_review": True,
    }
    result = _verify_investigation_result(raw, log, classification="UNMATCHED_BATCH")
    assert result["investigation_status"] == "INSUFFICIENT_EVIDENCE"
    assert result["requires_human_review"] is True


def test_identifier_strings_like_order_ref_are_not_treated_as_fabricated_numbers():
    """A claim mentioning 'OD-02-0001' must not be flagged just because the
    string contains digits -- order_ref is an identifier, not an amount."""
    log = _log(("get_order", {"order_ref": "OD-02-0001"}, {"order_ref": "OD-02-0001", "amount": 8871.61}))
    raw = {
        "hypothesis": "Order OD-02-0001 has amount Rs.8871.61.",
        "verified_facts": ["Order OD-02-0001's amount is Rs.8871.61."],
        "unverified_claims": [], "contradictions": [],
        "possible_root_cause": "", "recommended_next_step": "", "confidence_basis": "",
        "investigation_status": "VERIFIED_EXPLANATION", "requires_human_review": False,
    }
    result = _verify_investigation_result(raw, log, classification="TIMING_DIFFERENCE")
    assert result["verified_facts"] == ["Order OD-02-0001's amount is Rs.8871.61."]
    assert result["contradictions"] == []


def test_requires_human_review_true_for_actionable_classification_even_when_verified():
    """AI confidence must not override the deterministic requires_approval
    rule -- a fully-verified MISSING_REFUND_RECORD investigation still
    requires human review, because that classification always does."""
    log = _log(("get_exception_evidence", {"exception_id": 1}, {"unexplained_amount": 5786.83}))
    raw = {
        "hypothesis": "ok", "verified_facts": [], "unverified_claims": [], "contradictions": [],
        "possible_root_cause": "", "recommended_next_step": "", "confidence_basis": "",
        "investigation_status": "VERIFIED_EXPLANATION", "requires_human_review": False,  # AI says no
    }
    result = _verify_investigation_result(raw, log, classification="MISSING_REFUND_RECORD")
    assert result["requires_human_review"] is True  # verifier overrides the AI's own claim


def test_requires_human_review_false_only_for_verified_non_actionable_classification():
    log = _log(
        ("get_bank_candidates", {"batch_id": 4},
         {"batch_id": 4, "candidates": [{"date_diff_days": 2, "confidence_score": 0.85}]}),
        ("calculate_bridge", {"batch_id": 4}, {"batch_id": 4, "variance": 0.0}),
    )
    raw = {
        "hypothesis": "Bank credit is 2 days after settlement, within tolerance.",
        "verified_facts": [], "unverified_claims": [], "contradictions": [],
        "possible_root_cause": "", "recommended_next_step": "", "confidence_basis": "",
        "investigation_status": "VERIFIED_EXPLANATION", "requires_human_review": True,
    }
    result = _verify_investigation_result(raw, log, classification="TIMING_DIFFERENCE")
    assert result["requires_human_review"] is False


def test_malformed_string_instead_of_list_field_fails_clean_not_character_by_character():
    """Regression test for a real, observed model quirk: Claude Haiku
    occasionally serialized verified_facts as a single XML-tag-wrapped string
    instead of a JSON array, despite the tool schema declaring it an array of
    strings. Iterating a string in Python walks it character-by-character --
    this must be caught as malformed and fail clean (HUMAN_REVIEW_REQUIRED),
    never silently produce garbage single-character "claims"."""
    log = _log(("get_exception_evidence", {"exception_id": 1}, {"unexplained_amount": 5786.83}))
    raw = {
        "hypothesis": "ok",
        "verified_facts": "\n<item>Settlement entry 18 declares a refund of Rs.5786.83</item>\n",
        "unverified_claims": [], "contradictions": [],
        "possible_root_cause": "", "recommended_next_step": "", "confidence_basis": "",
        "investigation_status": "VERIFIED_EXPLANATION", "requires_human_review": False,
    }
    result = _verify_investigation_result(raw, log, classification="MISSING_REFUND_RECORD")
    assert result["investigation_status"] == "HUMAN_REVIEW_REQUIRED"
    assert result["verified_facts"] == []
    assert len(result["contradictions"]) == 1
    assert "malformed" in result["contradictions"][0].lower()
    assert result["requires_human_review"] is True


def test_self_computed_number_cannot_be_laundered_through_verify_amount_relationship():
    """Regression test for a real, observed failure mode: the AI computed a
    number itself (a baseline rate * gross amount -- real arithmetic, which
    it must never do) and passed it into verify_amount_relationship as one of
    the two amounts being compared. That tool faithfully echoes its inputs
    back in its result -- without this guard, the echoed self-computed number
    would be harvested as "evidence a tool returned" and pass grounding. Only
    numbers that appear in an actual DATA-source tool's result may ever be
    cited as verified; a number that only exists because the AI handed it to
    a comparison tool and got it echoed back must still be rejected."""
    log = _log(
        ("get_settlement_batch", {"batch_id": 9},
         {"batch_id": 9, "total_fees": 4670.51, "total_gross": 156623.30}),
        ("verify_amount_relationship",
         {"amount_a": 4670.51, "amount_b": 3871.17, "label_a": "actual fees", "label_b": "baseline expected fees"},
         {"label_a": "actual fees", "amount_a": 4670.51, "label_b": "baseline expected fees",
          "amount_b": 3871.17, "difference": 799.34, "tolerance_rupees": 0.0, "within_tolerance": False}),
    )
    raw = {
        "hypothesis": "ok",
        "verified_facts": ["Expected fees at the baseline rate would be Rs.3871.17."],
        "unverified_claims": [], "contradictions": [],
        "possible_root_cause": "", "recommended_next_step": "", "confidence_basis": "",
        "investigation_status": "VERIFIED_EXPLANATION", "requires_human_review": False,
    }
    result = _verify_investigation_result(raw, log, classification="SYSTEMIC_FEE_DRIFT")
    assert result["verified_facts"] == []
    assert len(result["contradictions"]) == 1
    assert "3871.17" in result["contradictions"][0]
    assert result["investigation_status"] == "CONTRADICTED"


def test_ratio_expressed_as_percentage_is_grounded_not_flagged():
    """Regression test: get_exception_evidence's anomaly comparison returns
    fee_rate as a raw ratio (0.02982), but the model naturally writes this as
    a percentage (2.982%) for readability -- the same fact, not a new one.
    Found live: this was being rejected as fabricated, burying genuine
    catches (self-counted totals, self-computed differences) under a false
    positive caused by unit presentation, not content."""
    log = _log(
        ("get_exception_evidence", {"exception_id": 7},
         {"comparison": {"batch_value": 0.02982, "baseline_mean": 0.024716}}),
    )
    raw = {
        "hypothesis": "ok",
        "verified_facts": [
            "The batch's fee rate is 2.982%.",
            "The baseline mean fee rate is 2.4716%.",
        ],
        "unverified_claims": [], "contradictions": [],
        "possible_root_cause": "", "recommended_next_step": "", "confidence_basis": "",
        "investigation_status": "VERIFIED_EXPLANATION", "requires_human_review": False,
    }
    result = _verify_investigation_result(raw, log, classification="SYSTEMIC_FEE_DRIFT")
    assert len(result["verified_facts"]) == 2
    assert result["contradictions"] == []


def test_self_counted_total_is_still_rejected_even_though_numerically_correct():
    """A count the AI derives by counting a list's length (rather than
    reading an explicit count field) must still be rejected, even if the
    count happens to be numerically correct -- the AI performed a
    computation (counting) it wasn't supposed to do, and next time the count
    might be wrong with no way to tell the difference."""
    # IDs deliberately don't include 15 anywhere (real batch 9 entries start at
    # 140+) -- otherwise "15" could coincidentally be grounded via an entry's
    # own id, defeating the point of this test (proving the COUNT itself, not
    # any id, is what must be rejected).
    log = _log(
        ("get_settlement_entries", {"batch_id": 9},
         [{"settlement_entry_id": i} for i in range(140, 155)]),
    )
    raw = {
        "hypothesis": "ok",
        "verified_facts": ["Batch 9 has 15 settlement entries."],
        "unverified_claims": [], "contradictions": [],
        "possible_root_cause": "", "recommended_next_step": "", "confidence_basis": "",
        "investigation_status": "VERIFIED_EXPLANATION", "requires_human_review": False,
    }
    result = _verify_investigation_result(raw, log, classification="SYSTEMIC_FEE_DRIFT")
    assert result["verified_facts"] == []
    assert result["investigation_status"] == "CONTRADICTED"


def test_partially_verified_when_some_claims_survive_and_some_dont():
    log = _log(("get_settlement_batch", {"batch_id": 2}, {"batch_id": 2, "total_net": 116294.35}))
    raw = {
        "hypothesis": "ok",
        "verified_facts": ["total_net is Rs.116294.35"],
        "unverified_claims": ["Possibly related to a fee tier change, unconfirmed."],
        "contradictions": [],
        "possible_root_cause": "", "recommended_next_step": "", "confidence_basis": "",
        "investigation_status": "VERIFIED_EXPLANATION", "requires_human_review": False,
    }
    result = _verify_investigation_result(raw, log, classification="FEE_TIER_MISMATCH")
    assert result["investigation_status"] == "PARTIALLY_VERIFIED"
