"""
AI explanation layer for ExceptionRecords. This is the ONLY place in the codebase
that calls an LLM (Anthropic Claude). Per CLAUDE.md: AI never performs or touches
arithmetic -- it only restates numbers that deterministic code already computed.
generate_explanation() takes no database session and imports nothing from
app.models: it can only see the plain dict data explicitly passed to it, and
cannot query anything beyond that.

Every AI response is validated before use: every number the model states must match
(within Rs.0.01) a number that was actually present in the input data. Any response
containing an unverifiable number is discarded entirely in favor of a safe fallback
string built from the same deterministic facts -- this function can never surface a
number that didn't come from the database.
"""
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set

from dotenv import load_dotenv

load_dotenv()

import anthropic

logger = logging.getLogger("ledgertrail.ai_explain")

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 500  # 300 was confirmed too tight for MISSING_REFUND_RECORD's prompt
REQUEST_TIMEOUT_SECONDS = 15.0
NUMBER_TOLERANCE = 0.01

SYSTEM_PROMPT = (
    "You are explaining a financial discrepancy that has ALREADY been calculated by "
    "deterministic code. You must NEVER calculate, estimate, round, or introduce any "
    "number that is not explicitly given to you in the data below. Only restate and "
    "explain the given facts in plain language for a non-technical reader. If you are "
    "unsure of a number, omit it rather than guess. "
    "Write your response as plain prose sentences only. Do not use Markdown "
    "formatting -- no headers (#), no bold (**), no bullet points or numbered lists "
    "with special characters. If listing steps, write them as a plain sentence like "
    "'First do X, then do Y.'"
)

# Matches "Rs.5,786.83", "₹5,786.83", "5786.83", "23", etc. Deliberately loose --
# a false positive here just means a safe response gets discarded unnecessarily
# (fail closed), which is the correct default per the module docstring above.
NUMBER_RE = re.compile(r"[₹]?\s*\d[\d,]*(?:\.\d+)?")

# "1. ", "2. " etc. at the start of a line -- markdown ordered-list markers, not amounts.
LIST_MARKER_RE = re.compile(r"(?m)^\s*\d+\.\s+")

# "(1)", "(2)", "(3)" etc. anywhere inline -- parenthetical enumeration markers
# used in ordinary prose ("This could happen because: (1) ..., (2) ..., (3) ..."),
# not amounts. Found live in an investigation report (app/investigation_agent.py)
# that got wrongly flagged CONTRADICTED over "(3)" in an enumerated root-cause
# explanation. Currency in every system prompt in this codebase is always written
# "Rs.X"/"₹X", never a bare parenthesized number, so stripping small parenthesized
# integers here removes a formatting artifact, never a real amount.
INLINE_ENUM_MARKER_RE = re.compile(r"\(\d{1,2}\)")


@dataclass
class ExplanationResult:
    text: str
    source: str  # "ai_generated" | "fallback"


def _fallback_text(exception_record: Dict[str, Any]) -> str:
    return (
        f"This exception involves a {exception_record['classification']} discrepancy "
        f"of ₹{exception_record['unexplained_amount']}. {exception_record['suggested_action']}"
    )


def _collect_allowed_numbers(exception_record: Dict[str, Any], evidence_data: Dict[str, Any]) -> Set[float]:
    allowed = set()

    if exception_record.get("unexplained_amount") is not None:
        allowed.add(round(float(exception_record["unexplained_amount"]), 2))

    for entry in evidence_data.get("settlement_entries", []):
        for field in ("gross_amount", "fee", "tax", "refund", "net_amount"):
            if entry.get(field) is not None:
                allowed.add(round(float(entry[field]), 2))

    for order in evidence_data.get("order_records", []):
        for field in ("amount", "refund_amount", "fee_amount"):
            if order.get(field) is not None:
                allowed.add(round(float(order[field]), 2))

    for txn in evidence_data.get("bank_transactions", []):
        if txn.get("amount") is not None:
            allowed.add(round(float(txn["amount"]), 2))
        if txn.get("date"):
            allowed |= _date_component_numbers(txn["date"])

    return allowed


def _date_component_numbers(date_str: str) -> Set[float]:
    """Numeric components (year/month/day) of a YYYY-MM-DD date string. The prompt
    states dates in ISO format, but the model sometimes reformats them in prose
    (e.g. "August 29, 2026" instead of "2026-08-29") -- exact-string identifier
    stripping misses that, so the day/month/year would otherwise look like an
    unverifiable number. This only tolerates the exact digits of a date we already
    gave the model, never a broader class of numbers."""
    numbers = set()
    parts = str(date_str).split("-")
    if len(parts) == 3:
        try:
            year, month, day = (int(p) for p in parts)
            numbers.update({float(year), float(month), float(day)})
        except ValueError:
            pass
    return numbers


def _collect_identifier_strings(evidence_data: Dict[str, Any]) -> Set[str]:
    """Non-monetary strings (order refs, bank references, dates) that legitimately
    contain digits but are not amounts subject to the number-hallucination check."""
    identifiers = set()

    for entry in evidence_data.get("settlement_entries", []):
        if entry.get("order_ref"):
            identifiers.add(str(entry["order_ref"]))

    for order in evidence_data.get("order_records", []):
        if order.get("order_ref"):
            identifiers.add(str(order["order_ref"]))

    for txn in evidence_data.get("bank_transactions", []):
        if txn.get("reference"):
            identifiers.add(str(txn["reference"]))
        if txn.get("date"):
            identifiers.add(str(txn["date"]))

    return identifiers


def _extract_numbers(text: str) -> list:
    # Strip markdown ordered-list markers ("1. ", "2. " at the start of a line)
    # and inline parenthetical enumeration markers ("(1)", "(2)" anywhere in the
    # text) before scanning for numbers -- otherwise list/enumeration numbering
    # reads as literal values (1.0, 2.0, ...), which is a formatting artifact,
    # not a stated amount.
    text = LIST_MARKER_RE.sub("", text)
    text = INLINE_ENUM_MARKER_RE.sub("", text)

    numbers = []
    for match in NUMBER_RE.finditer(text):
        raw = match.group().replace("₹", "").replace(",", "").strip()
        if not raw or raw == ".":
            continue
        try:
            numbers.append(float(raw))
        except ValueError:
            continue
    return numbers


def _validate_response(text: str, allowed_numbers: Set[float], identifier_strings: Set[str]):
    """Returns (is_valid, offending_number_or_None)."""
    cleaned = text
    for ident in sorted(identifier_strings, key=len, reverse=True):
        cleaned = cleaned.replace(ident, " ")

    for n in _extract_numbers(cleaned):
        if not any(abs(n - allowed) <= NUMBER_TOLERANCE for allowed in allowed_numbers):
            return False, n
    return True, None


def _build_prompt(exception_record: Dict[str, Any], evidence_data: Dict[str, Any]) -> str:
    lines = [
        f"Classification: {exception_record['classification']}",
        f"Unexplained amount: Rs.{exception_record['unexplained_amount']}",
        f"Suggested action: {exception_record['suggested_action']}",
        "",
        "Evidence:",
    ]

    for entry in evidence_data.get("settlement_entries", []):
        lines.append(
            f"- Settlement entry {entry.get('order_ref')}: gross=Rs.{entry.get('gross_amount')}, "
            f"fee=Rs.{entry.get('fee')}, tax=Rs.{entry.get('tax')}, refund=Rs.{entry.get('refund')}, "
            f"net=Rs.{entry.get('net_amount')}"
        )

    for order in evidence_data.get("order_records", []):
        lines.append(
            f"- Order record {order.get('order_ref')}: amount=Rs.{order.get('amount')}, "
            f"status={order.get('status')}, refund_amount=Rs.{order.get('refund_amount')}, "
            f"fee_amount=Rs.{order.get('fee_amount')}"
        )

    for txn in evidence_data.get("bank_transactions", []):
        lines.append(
            f"- Bank transaction: amount=Rs.{txn.get('amount')}, date={txn.get('date')}, "
            f"reference={txn.get('reference')}"
        )

    return "\n".join(lines)


def generate_explanation(exception_record: Dict[str, Any], evidence_data: Dict[str, Any]) -> ExplanationResult:
    """
    exception_record: {"classification": str, "unexplained_amount": float, "suggested_action": str}
    evidence_data: {"settlement_entries": [...], "order_records": [...], "bank_transactions": [...]}
    (the same shape as GET /batches/{id}/evidence's response, scoped to one exception)
    """
    fallback = _fallback_text(exception_record)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("path=fallback reason=no_api_key classification=%s", exception_record.get("classification"))
        return ExplanationResult(text=fallback, source="fallback")

    allowed_numbers = _collect_allowed_numbers(exception_record, evidence_data)
    identifier_strings = _collect_identifier_strings(evidence_data)
    prompt = _build_prompt(exception_record, evidence_data)

    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS)
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            # cache_control: SYSTEM_PROMPT is identical on every call this
            # function makes. Correctly configured (see
            # investigation_agent.py's cache_control comments for how that
            # was confirmed working in principle), but this system prompt
            # alone is well under investigation_agent.py's ~2903-token
            # prefix, which itself already measured below the cacheable
            # minimum -- so this is a no-op in practice at its current
            # length. Kept because it's free and correct. See CLAUDE.md's
            # "Prompt caching" note.
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        stop_reason = response.stop_reason
    except Exception:
        logger.exception("path=fallback reason=api_error classification=%s", exception_record.get("classification"))
        return ExplanationResult(text=fallback, source="fallback")

    # A non-"end_turn" stop_reason (e.g. "max_tokens": cut off at the token budget)
    # means the response is incomplete. Reject it even if it happens to contain zero
    # numbers to flag -- a truncated response with no numbers would otherwise pass
    # the numeric check despite not actually being a trustworthy, complete answer.
    # Carried forward from the Gemini version, where this exact gap was found: a
    # response cut off before stating any numbers silently passed validation.
    if stop_reason is not None and stop_reason != "end_turn":
        logger.warning(
            "path=fallback reason=incomplete_response stop_reason=%s classification=%s",
            stop_reason, exception_record.get("classification"),
        )
        return ExplanationResult(text=fallback, source="fallback")

    if not text:
        logger.warning("path=fallback reason=empty_response classification=%s", exception_record.get("classification"))
        return ExplanationResult(text=fallback, source="fallback")

    is_valid, bad_number = _validate_response(text, allowed_numbers, identifier_strings)
    if not is_valid:
        logger.warning(
            "path=fallback reason=unverifiable_number offending_number=%s classification=%s",
            bad_number, exception_record.get("classification"),
        )
        return ExplanationResult(text=fallback, source="fallback")

    logger.info("path=ai_generated classification=%s", exception_record.get("classification"))
    return ExplanationResult(text=text, source="ai_generated")
