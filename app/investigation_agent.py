"""
Phase C: the bounded AI Reconciliation Investigation Agent. Ties Phase B's
tool layer (app/investigation_tools.py) to a real Anthropic tool-use loop,
producing a structured investigation report that a deterministic verifier
checks BEFORE it's trusted -- same "propose then verify" principle as
app/ai_explain.py and app/nl_query.py, generalized from a single number-
hallucination check to a full structured, multi-field result.

Architecture (mirrors the spec's own diagram):

    exception -> AI decides what evidence it needs -> AI calls read-only
    tools -> LedgerTrail returns authoritative evidence -> AI forms a
    structured hypothesis (via the submit_investigation_report tool) ->
    THIS MODULE'S VERIFIER checks every claim against the evidence actually
    returned -> AI produces an investigation report -> human decides.

The single most important design rule, stated once so it's easy to audit:
THE AI'S SELF-REPORTED investigation_status AND requires_human_review ARE
NEVER THE FINAL VALUES. _verify_investigation_result() always recomputes
both from the actual evidence and the actual verified/contradicted claims --
"AI confidence must NOT override deterministic evidence" (spec item 4) is
enforced structurally here, not just as a prompting convention. The AI's own
self-assessment is kept (as ai_self_reported_status) for transparency and for
the adversarial-case demo, but it is advisory input to the verifier, not the
answer.

_verify_investigation_result() is a PURE function -- no Anthropic client, no
network call, callable with a synthetic AI result and synthetic evidence.
That's deliberate: it's what makes "does the verifier reject an unsupported
claim" a fast, deterministic, free regression test (see
tests/test_investigation_agent.py) instead of something that can only be
checked by hitting the live API and hoping for a particular answer.
"""
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from dotenv import load_dotenv

load_dotenv()

import anthropic

from app import investigation_tools as tools
from app import models
from app.ai_explain import _date_component_numbers, _extract_numbers
from app.exceptions import CLASSIFICATION_INFO
from app.money import paise_to_rupees

logger = logging.getLogger("ledgertrail.investigation_agent")

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 2048  # 1024 confirmed too tight empirically: the final
# submit_investigation_report call packs 7+ substantial prose fields
# (hypothesis, several verified_facts, root_cause, next_step, confidence_basis)
# into one response and hit stop_reason="max_tokens" mid-report. Same failure
# mode ai_explain.py hit with its own token budget (300 -> 500) -- found by
# actually running it, not by estimating in advance.
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_TOOL_CALLS = 8  # bounded, per spec -- not counting the final submit_investigation_report
# call. Started at 6; raised after an aggregate-classification investigation
# (SYSTEMIC_FEE_DRIFT) genuinely ran out of budget mid-investigation before the
# get_exception_evidence description above was clarified -- kept a little
# headroom above what record-level cases have needed (6, organically) rather
# than tuning it to the exact minimum, since "bounded" doesn't have to mean
# "as tight as possible".
NUMBER_TOLERANCE = 0.01

VALID_STATUSES = (
    "VERIFIED_EXPLANATION", "PARTIALLY_VERIFIED", "INSUFFICIENT_EVIDENCE",
    "CONTRADICTED", "HUMAN_REVIEW_REQUIRED",
)

SYSTEM_PROMPT = (
    "You are a bounded financial reconciliation investigation assistant for "
    "LedgerTrail. You investigate ONE settlement exception at a time using the "
    "read-only tools provided. You NEVER calculate, sum, or estimate any number "
    "yourself -- every number in your final report must come directly from a tool "
    "result you actually received in this conversation. This includes counting: "
    "if you want to state how many of something there are (e.g. how many "
    "settlement entries a batch has), only state a count if a tool result gave "
    "you that count explicitly -- do not count list items yourself and state that "
    "number. It also includes derived figures like percentage-point differences: "
    "use verify_amount_relationship for any comparison, never compute a difference "
    "in your head. You NEVER invent evidence: "
    "if a tool returns null, empty, or 'not found', say so plainly rather than "
    "guessing. You do not decide whether to approve or reject anything -- that is "
    "always a human decision; your job is to investigate and explain, not to act. "
    "Use the tools to gather whatever evidence is genuinely relevant to this "
    "exception -- you may call as few or as many as you need, within the limit "
    "you're given. When you have gathered enough evidence (or determined you "
    "cannot), call submit_investigation_report exactly once to conclude. "
    "If the evidence is genuinely insufficient to explain the exception, set "
    "investigation_status to INSUFFICIENT_EVIDENCE and say so plainly instead of "
    "inventing a plausible-sounding explanation -- this is correct, expected "
    "behavior when the evidence doesn't support a conclusion, not a failure. "
    "verified_facts must be facts you personally confirmed via an actual tool "
    "result in this conversation, not things you assume, infer, or consider "
    "likely -- if you are not certain a claim is directly supported by a tool "
    "result you received, put it in unverified_claims instead. Write plain prose "
    "only, no Markdown formatting. "
    "You have a limited number of tool calls -- use them efficiently. For a "
    "SYSTEMIC_FEE_DRIFT or SYSTEMIC_REFUND_DRIFT exception specifically: this is a "
    "batch-wide statistical finding, not attributable to any single order. Do not "
    "call get_order or get_refunds repeatedly for individual orders within the "
    "batch looking for the cause -- no single order explains a batch-wide rate "
    "shift, and doing this will exhaust your tool budget without adding evidence. "
    "get_exception_evidence's comparison (batch rate vs. baseline mean/stdev) is "
    "the evidence for this classification; calculate_bridge and get_settlement_batch "
    "are enough to additionally confirm the batch itself is otherwise consistent."
)

# --- Tool schemas (Anthropic tool-use format) --------------------------------

_TOOL_SCHEMAS = [
    {
        "name": "get_settlement_batch",
        "description": "Fetches one settlement batch's declared totals (gross, refunds, "
        "fees, tax, net) and whether it's matched to a bank transaction.",
        "input_schema": {
            "type": "object",
            "properties": {"batch_id": {"type": "integer"}},
            "required": ["batch_id"],
        },
    },
    {
        "name": "get_settlement_entries",
        "description": "Fetches every order-level settlement entry (gross, fee, tax, "
        "refund, net) belonging to one batch.",
        "input_schema": {
            "type": "object",
            "properties": {"batch_id": {"type": "integer"}},
            "required": ["batch_id"],
        },
    },
    {
        "name": "get_bank_candidates",
        "description": "Fetches every bank transaction that is a plausible amount+date "
        "candidate match for a batch (the same logic the real matcher uses), showing "
        "which one (if any) was actually chosen and why the others weren't.",
        "input_schema": {
            "type": "object",
            "properties": {"batch_id": {"type": "integer"}},
            "required": ["batch_id"],
        },
    },
    {
        "name": "get_order",
        "description": "Fetches one merchant order record by its order_ref (e.g. "
        "'OD-02-0001') -- amount, status, refund_amount, fee_amount as tracked "
        "internally by the merchant, separate from what the settlement file says.",
        "input_schema": {
            "type": "object",
            "properties": {"order_ref": {"type": "string"}},
            "required": ["order_ref"],
        },
    },
    {
        "name": "get_refunds",
        "description": "For one order_ref, returns the order record's own refund_amount "
        "AND every settlement entry's refund figure for that same order_ref side by "
        "side, so you can compare them -- it does not decide whether they agree.",
        "input_schema": {
            "type": "object",
            "properties": {"order_ref": {"type": "string"}},
            "required": ["order_ref"],
        },
    },
    {
        "name": "get_exception_evidence",
        "description": "Fetches the evidence already linked to a specific exception at "
        "the time it was classified -- either resolved settlement/order/bank records, "
        "or (for SYSTEMIC_FEE_DRIFT/SYSTEMIC_REFUND_DRIFT) the statistical comparison "
        "against the baseline. This is the fastest way to see what the deterministic "
        "engine itself already found for this exception. For SYSTEMIC_FEE_DRIFT/"
        "SYSTEMIC_REFUND_DRIFT specifically: the returned comparison (batch rate vs. "
        "baseline mean/stdev) IS the evidence -- it's a batch-wide statistical finding, "
        "not attributable to any single order, so inspecting individual orders one by "
        "one is unlikely to explain it and will exhaust your tool budget without "
        "adding useful evidence.",
        "input_schema": {
            "type": "object",
            "properties": {"exception_id": {"type": "integer"}},
            "required": ["exception_id"],
        },
    },
    {
        "name": "calculate_bridge",
        "description": (
            "Computes the reconciliation bridge for one batch: does the sum of its "
            "settlement entries match its declared total (internal consistency), and "
            "does that declared total match the matched bank credit (external "
            "reconciliation). IMPORTANT: the returned is_reconciled field means only "
            "that the batch's declared total matches its bank credit exactly -- it "
            "does NOT mean the batch is free of open exceptions. A batch can show "
            "is_reconciled: true here while still having unresolved exceptions "
            "requiring human review. Do not state or imply in your report that a "
            "batch is fully resolved or closed based on this field alone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"batch_id": {"type": "integer"}},
            "required": ["batch_id"],
        },
    },
    {
        "name": "verify_amount_relationship",
        "description": "Deterministically compares two rupee amounts you name and "
        "returns their exact difference and whether it's within a tolerance you "
        "specify (default 0, i.e. exact equality). Use this instead of doing "
        "subtraction yourself -- you must never calculate a difference on your own. "
        "CRITICAL: amount_a and amount_b must each be a value you read directly from "
        "an earlier tool result in this conversation -- never a number you calculated "
        "yourself (e.g. multiplying a rate by a total, or any other arithmetic). If "
        "you need to compare something against a rate or a derived figure, request "
        "the specific tool that can give you that figure directly rather than "
        "computing it. This tool cannot tell whether its inputs were genuinely sourced "
        "or invented -- that responsibility is entirely yours.",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount_a": {"type": "number"},
                "amount_b": {"type": "number"},
                "label_a": {"type": "string", "description": "what amount_a represents"},
                "label_b": {"type": "string", "description": "what amount_b represents"},
                "tolerance_rupees": {"type": "number", "description": "defaults to 0"},
            },
            "required": ["amount_a", "amount_b", "label_a", "label_b"],
        },
    },
    {
        "name": "verify_reference_relationship",
        "description": "Deterministically checks whether one reference string (e.g. an "
        "order_ref or a UTR) appears inside another piece of text (e.g. a bank "
        "narration). Case-insensitive substring match, nothing fuzzy or semantic.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reference_value": {"type": "string"},
                "candidate_text": {"type": "string"},
            },
            "required": ["reference_value", "candidate_text"],
        },
    },
    {
        "name": "verify_narration",
        "description": "Checks whether a bank transaction's narration text is "
        "consistent with a genuine Razorpay settlement credit (mentions a bank name, "
        "a UTR-style reference, and 'RAZORPAY SETTLEMENT'), cross-checked against a "
        "deterministic keyword rule.",
        "input_schema": {
            "type": "object",
            "properties": {"bank_transaction_id": {"type": "integer"}},
            "required": ["bank_transaction_id"],
        },
    },
    {
        "name": "submit_investigation_report",
        "description": "Concludes the investigation. Call this exactly once, when you "
        "have gathered enough evidence (or determined you cannot) to report your "
        "findings. This ends the investigation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hypothesis": {"type": "string"},
                "verified_facts": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Facts you personally confirmed via a tool result in this conversation.",
                },
                "unverified_claims": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Things you suspect but could not confirm via a tool result.",
                },
                "contradictions": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Anything you found that contradicts your own hypothesis or an earlier claim.",
                },
                "possible_root_cause": {"type": "string"},
                "recommended_next_step": {"type": "string"},
                "confidence_basis": {"type": "string", "description": "Why you believe what you believe, in plain language."},
                "investigation_status": {"type": "string", "enum": list(VALID_STATUSES)},
                "requires_human_review": {"type": "boolean"},
            },
            "required": [
                "hypothesis", "verified_facts", "unverified_claims", "contradictions",
                "possible_root_cause", "recommended_next_step", "confidence_basis",
                "investigation_status", "requires_human_review",
            ],
        },
    },
]

_TOOL_NAMES = {t["name"] for t in _TOOL_SCHEMAS}


@dataclass
class ToolCallRecord:
    tool: str
    input: Dict[str, Any]
    result: Any


@dataclass
class InvestigationResult:
    exception_id: int
    investigation_status: str
    hypothesis: str
    evidence_used: List[str]
    tool_calls: List[Dict[str, Any]]
    verified_facts: List[str]
    unverified_claims: List[str]
    contradictions: List[str]
    possible_root_cause: Optional[str]
    recommended_next_step: Optional[str]
    confidence_basis: Optional[str]
    requires_human_review: bool
    ai_self_reported_status: Optional[str] = None
    source: str = "ai_investigated"  # "ai_investigated" | "fallback"


def _fallback_result(
    exception_id: int, reason: str, tool_call_log: Optional[List[ToolCallRecord]] = None
) -> InvestigationResult:
    """tool_call_log is preserved (not discarded) whenever the caller has one --
    a budget-exhausted or errored-out investigation may still have gathered
    real, useful evidence along the way; showing it is more honest and more
    useful than silently throwing it away just because no final report was
    ever submitted."""
    logger.warning("path=fallback reason=%s exception_id=%s", reason, exception_id)
    tool_call_log = tool_call_log or []
    return InvestigationResult(
        exception_id=exception_id,
        investigation_status="HUMAN_REVIEW_REQUIRED",
        hypothesis="",
        evidence_used=[r.tool for r in tool_call_log],
        tool_calls=[{"tool": r.tool, "input": r.input, "result": r.result} for r in tool_call_log],
        verified_facts=[],
        unverified_claims=[],
        contradictions=[],
        possible_root_cause=None,
        recommended_next_step="Investigation could not be completed automatically; review manually.",
        confidence_basis=f"Investigation did not complete: {reason}.",
        requires_human_review=True,
        source="fallback",
    )


# --- Pure verifier (no network call; independently unit-testable) -----------

def _collect_numeric_leaves(obj: Any, out: Set[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        value = float(obj)
        out.add(round(value, 2))
        # Ratio-to-percentage is a lossless unit conversion, not a calculation
        # in the sense the system prompt forbids -- get_bank_candidates'
        # confidence_score and the anomaly comparison's rates/fee_rate are
        # returned as plain ratios (0.02982), but the model naturally writes
        # them as percentages (2.982%) for readability, which is the SAME
        # fact restated, not a new one. Found live: without this, a correct,
        # faithful percentage rendering of a genuine tool-returned ratio was
        # being rejected as "fabricated" alongside actually-fabricated numbers,
        # burying real catches under false ones. Scoped to values already in
        # (0, 1) so this doesn't broadly loosen grounding for ids/amounts/counts.
        if 0 < abs(value) < 1:
            out.add(round(value * 100, 2))
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_numeric_leaves(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_numeric_leaves(v, out)


def _collect_identifier_strings(obj: Any, out: Set[str]) -> None:
    """Non-numeric strings that legitimately contain digits (order refs, UTRs,
    dates, descriptions) -- stripped from narrative text before number
    extraction, same principle as ai_explain.py's identifier handling."""
    if isinstance(obj, str):
        out.add(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_identifier_strings(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_identifier_strings(v, out)


# verify_amount_relationship/verify_reference_relationship are comparison
# UTILITIES, not data sources -- they echo their own caller-supplied inputs
# back in their result (so the caller can see what was compared). That echo
# must NOT be harvested as new grounding evidence: found live (not
# hypothesized) that the model can compute a number itself (e.g. a baseline
# rate multiplied by a gross amount -- real arithmetic, forbidden by the
# system prompt) and pass it into verify_amount_relationship as amount_b; the
# tool faithfully echoes 3871.17 back in its result, and without this
# exclusion that self-computed number would be laundered into "evidence a
# tool returned" and pass the grounding check. Excluding these two tools'
# results from harvesting closes that loophole: an amount is only ever
# grounded if it appeared in an actual DATA-source tool's result, never
# merely because the AI handed it to a comparison tool and got it echoed back.
_NON_EVIDENCE_TOOLS = {"verify_amount_relationship", "verify_reference_relationship"}


def _build_evidence_context(tool_call_log: List[ToolCallRecord]) -> (Set[float], Set[str]):
    """Every number and every identifier-like string that appeared anywhere in
    an actual data-source tool result during this investigation -- the ONLY
    things the AI's final report is allowed to state as fact."""
    allowed_numbers: Set[float] = set()
    identifier_strings: Set[str] = set()

    for record in tool_call_log:
        if record.tool in _NON_EVIDENCE_TOOLS:
            continue
        _collect_numeric_leaves(record.result, allowed_numbers)
        _collect_identifier_strings(record.result, identifier_strings)
        for date_str in _find_date_strings(record.result):
            allowed_numbers |= _date_component_numbers(date_str)

    return allowed_numbers, identifier_strings


def _find_date_strings(obj: Any) -> List[str]:
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("date", "settlement_date") and isinstance(v, str):
                found.append(v)
            else:
                found.extend(_find_date_strings(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found.extend(_find_date_strings(v))
    return found


def _claim_is_grounded(claim: str, allowed_numbers: Set[float], identifier_strings: Set[str]) -> Optional[float]:
    """Returns the first ungrounded number found in `claim`, or None if every
    number in it traces back to something a tool actually returned."""
    cleaned = claim
    for ident in sorted(identifier_strings, key=len, reverse=True):
        if ident:
            cleaned = cleaned.replace(ident, " ")
    for n in _extract_numbers(cleaned):
        if not any(abs(n - allowed) <= NUMBER_TOLERANCE for allowed in allowed_numbers):
            return n
    return None


def _verify_investigation_result(
    raw_result: Dict[str, Any],
    tool_call_log: List[ToolCallRecord],
    classification: Optional[str] = None,
) -> Dict[str, Any]:
    """THE deterministic verifier. Pure function: no Anthropic client, no
    database, no side effects -- everything it needs is passed in. Returns a
    NEW dict with verified_facts/unverified_claims/contradictions/
    investigation_status/requires_human_review all recomputed from the actual
    evidence, never copied blindly from raw_result. This is what "AI
    confidence must NOT override deterministic evidence" means in code, not
    just in a system prompt.

    Defensively validates shape before trusting any of it: observed live (not
    hypothesized) that the model occasionally serializes a list-typed field as
    a single XML-tag-wrapped string instead of a real JSON array, despite the
    tool's input_schema declaring it an array of strings. Iterating a string
    in Python walks it character-by-character -- silently producing dozens of
    garbage single-character "claims" and false contradictions rather than a
    clean failure. Malformed shape is treated the same as any other failure
    mode: HUMAN_REVIEW_REQUIRED, nothing from the malformed field trusted."""
    for list_field in ("verified_facts", "unverified_claims", "contradictions"):
        value = raw_result.get(list_field)
        if value is not None and (
            not isinstance(value, list) or any(not isinstance(item, str) for item in value)
        ):
            logger.warning(
                "path=fallback reason=malformed_report_field field=%s type=%s",
                list_field, type(value).__name__,
            )
            return {
                "investigation_status": "HUMAN_REVIEW_REQUIRED",
                "verified_facts": [],
                "unverified_claims": [],
                "contradictions": [
                    f"Investigation report malformed: '{list_field}' was not a valid list of "
                    f"strings (got {type(value).__name__}). Discarded rather than parsed."
                ],
                "requires_human_review": True,
                "ai_self_reported_status": raw_result.get("investigation_status"),
            }

    allowed_numbers, identifier_strings = _build_evidence_context(tool_call_log)

    verified_facts: List[str] = []
    contradictions: List[str] = list(raw_result.get("contradictions") or [])
    unverified_claims: List[str] = list(raw_result.get("unverified_claims") or [])

    for claim in raw_result.get("verified_facts") or []:
        bad_number = _claim_is_grounded(claim, allowed_numbers, identifier_strings)
        if bad_number is None:
            verified_facts.append(claim)
        else:
            contradictions.append(
                f"{claim} [REJECTED BY VERIFIER: states {bad_number}, which does not match "
                f"any number returned by a tool call in this investigation]"
            )

    # The narrative fields are held to the same standard -- a fabricated number
    # in the hypothesis/root-cause/next-step text is exactly the failure mode
    # this verifier exists to catch, not just numbers mislabeled as "verified".
    narrative_bad_numbers = []
    for field_name in ("hypothesis", "possible_root_cause", "recommended_next_step", "confidence_basis"):
        text = raw_result.get(field_name)
        if not text:
            continue
        bad = _claim_is_grounded(text, allowed_numbers, identifier_strings)
        if bad is not None:
            narrative_bad_numbers.append((field_name, bad))

    for field_name, bad_number in narrative_bad_numbers:
        contradictions.append(
            f"{field_name} states {bad_number}, which does not match any number returned "
            f"by a tool call in this investigation [FLAGGED BY VERIFIER]"
        )

    # NOTE: an empty verified_facts list is NOT by itself a downgrade signal --
    # a clean, fully-grounded investigation may legitimately have nothing that
    # needed individual fact-citation (e.g. "this is a timing difference, the
    # match is fuzzy" needs no separate verified_facts entry). Every claim that
    # WAS made but turned out ungrounded already landed in `contradictions`
    # above, so that branch alone (not "verified_facts ended up empty") is what
    # actually signals a problem.
    if contradictions:
        final_status = "CONTRADICTED"
    elif raw_result.get("investigation_status") == "INSUFFICIENT_EVIDENCE" and not verified_facts:
        # The AI's own abstention is respected when nothing contradicts it and
        # it isn't simultaneously claiming verified facts (which would be
        # self-inconsistent) -- abstaining is never a claim that needs grounding.
        final_status = "INSUFFICIENT_EVIDENCE"
    elif unverified_claims:
        final_status = "PARTIALLY_VERIFIED"
    else:
        final_status = "VERIFIED_EXPLANATION"

    # required_approval reflects the underlying exception's own classification
    # rule (CLASSIFICATION_INFO), same deterministic table every other part of
    # this app uses -- never derived from the AI's opinion.
    requires_approval = CLASSIFICATION_INFO.get(classification or "", {}).get("requires_approval", True)
    requires_human_review = (final_status != "VERIFIED_EXPLANATION") or requires_approval

    return {
        "investigation_status": final_status,
        "verified_facts": verified_facts,
        "unverified_claims": unverified_claims,
        "contradictions": contradictions,
        "requires_human_review": requires_human_review,
        "ai_self_reported_status": raw_result.get("investigation_status"),
    }


# --- Orchestration (the only part that touches the network) -----------------

def _dispatch_tool(db, name: str, tool_input: Dict[str, Any]) -> Any:
    if name == "get_settlement_batch":
        return tools.get_settlement_batch(db, tool_input["batch_id"])
    if name == "get_settlement_entries":
        return tools.get_settlement_entries(db, tool_input["batch_id"])
    if name == "get_bank_candidates":
        return tools.get_bank_candidates(db, tool_input["batch_id"])
    if name == "get_order":
        return tools.get_order(db, tool_input["order_ref"])
    if name == "get_refunds":
        return tools.get_refunds(db, tool_input["order_ref"])
    if name == "get_exception_evidence":
        return tools.get_exception_evidence(db, tool_input["exception_id"])
    if name == "calculate_bridge":
        return tools.calculate_bridge(db, tool_input["batch_id"])
    if name == "verify_amount_relationship":
        return tools.verify_amount_relationship(
            tool_input["amount_a"], tool_input["amount_b"],
            tool_input["label_a"], tool_input["label_b"],
            tool_input.get("tolerance_rupees", 0.0),
        )
    if name == "verify_reference_relationship":
        return tools.verify_reference_relationship(
            tool_input["reference_value"], tool_input["candidate_text"]
        )
    if name == "verify_narration":
        result = tools.verify_narration(db, tool_input["bank_transaction_id"])
        return (
            {
                "is_settlement_credit": result.is_settlement_credit,
                "confidence_note": result.confidence_note,
                "source": result.source,
            }
            if result is not None else None
        )
    raise ValueError(f"unknown tool: {name}")


def investigate_exception(db, exception_id: int) -> InvestigationResult:
    exc = db.query(models.ExceptionRecord).filter(models.ExceptionRecord.id == exception_id).first()
    if exc is None:
        return _fallback_result(exception_id, "exception_not_found")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return _fallback_result(exception_id, "no_api_key")

    starting_facts = {
        "exception_id": exc.id,
        "batch_id": exc.batch_id,
        "classification": exc.classification,
        "unexplained_amount": paise_to_rupees(exc.unexplained_amount),
        "status": exc.status,
        "suggested_action": exc.suggested_action,
    }
    user_message = (
        f"Investigate exception #{exc.id} on batch #{exc.batch_id}.\n"
        f"Classification: {exc.classification}\n"
        f"Unexplained amount: Rs.{starting_facts['unexplained_amount']}\n"
        f"Current status: {exc.status}\n"
        "Use the available tools to gather whatever evidence is genuinely relevant, "
        "then call submit_investigation_report to conclude."
    )

    messages = [{"role": "user", "content": user_message}]
    tool_call_log: List[ToolCallRecord] = []

    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("path=fallback reason=client_init_error exception_id=%s", exception_id)
        return _fallback_result(exception_id, "client_init_error")

    for turn in range(MAX_TOOL_CALLS + 1):
        try:
            response = client.messages.create(
                model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT,
                messages=messages, tools=_TOOL_SCHEMAS,
            )
        except Exception:
            logger.exception("path=fallback reason=api_error exception_id=%s turn=%s", exception_id, turn)
            return _fallback_result(exception_id, "api_error", tool_call_log)

        if response.stop_reason not in ("tool_use", "end_turn"):
            return _fallback_result(
                exception_id, f"incomplete_response_stop_reason_{response.stop_reason}", tool_call_log
            )

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

        if not tool_use_blocks:
            # Model responded in plain text instead of calling submit_investigation_report.
            text = "".join(b.text for b in response.content if b.type == "text")
            logger.warning("no_structured_report_submitted, model said: %r", text[:500])
            return _fallback_result(exception_id, "no_structured_report_submitted", tool_call_log)

        submit_block = next((b for b in tool_use_blocks if b.name == "submit_investigation_report"), None)

        if submit_block is not None:
            raw_result = submit_block.input
            verified = _verify_investigation_result(raw_result, tool_call_log, exc.classification)
            return InvestigationResult(
                exception_id=exc.id,
                investigation_status=verified["investigation_status"],
                hypothesis=raw_result.get("hypothesis", ""),
                evidence_used=[r.tool for r in tool_call_log],
                tool_calls=[
                    {"tool": r.tool, "input": r.input, "result": r.result} for r in tool_call_log
                ],
                verified_facts=verified["verified_facts"],
                unverified_claims=verified["unverified_claims"],
                contradictions=verified["contradictions"],
                possible_root_cause=raw_result.get("possible_root_cause"),
                recommended_next_step=raw_result.get("recommended_next_step"),
                confidence_basis=raw_result.get("confidence_basis"),
                requires_human_review=verified["requires_human_review"],
                ai_self_reported_status=verified["ai_self_reported_status"],
                source="ai_investigated",
            )

        if len(tool_call_log) >= MAX_TOOL_CALLS:
            return _fallback_result(exception_id, "tool_call_budget_exhausted", tool_call_log)

        # Execute every requested tool call for this turn and feed results back.
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in tool_use_blocks:
            if block.name not in _TOOL_NAMES:
                result = {"error": f"unknown tool {block.name}"}
            else:
                try:
                    result = _dispatch_tool(db, block.name, block.input)
                except Exception as e:
                    logger.exception("tool execution error: %s(%s)", block.name, block.input)
                    result = {"error": str(e)}
            tool_call_log.append(ToolCallRecord(tool=block.name, input=block.input, result=result))
            tool_results.append({
                "type": "tool_result", "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })
        messages.append({"role": "user", "content": tool_results})

    return _fallback_result(exception_id, "tool_call_budget_exhausted", tool_call_log)
