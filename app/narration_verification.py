"""
AI verification layer for bank transaction narrations. Same safety model as
app/ai_explain.py and app/nl_query.py: this module takes no database session and
imports nothing from app.models -- verify_narration() can only see the plain dict
explicitly passed to it (description, amount, date), nothing else.

This is a "propose then verify" feature, same as everywhere else the AI layer is
used: the AI proposes a YES/NO judgment, and a deterministic keyword check
independently verifies it. If the two disagree, the AI's response is discarded
and the deterministic result is used instead -- the AI is never trusted blindly,
same principle as the numeric-hallucination check in ai_explain.py, just applied
to a boolean claim instead of a set of numbers.
"""
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict

from dotenv import load_dotenv

load_dotenv()

import anthropic

logger = logging.getLogger("ledgertrail.narration_verification")

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 150
REQUEST_TIMEOUT_SECONDS = 15.0

SYSTEM_PROMPT = (
    "You are checking whether a bank transaction narration is consistent with being "
    "a genuine Razorpay settlement credit. A real Razorpay settlement credit "
    "narration typically follows the pattern: mentions a bank name, includes a "
    "UTR-style alphanumeric reference, and explicitly says something like 'RAZORPAY "
    "SETTLEMENT'. Respond with exactly YES or NO on the first line, followed by a "
    "one-sentence plain-language reason. Do not invent details not present in the "
    "narration text."
)

# UTR-like: a short optional letter prefix followed by a run of 6+ digits -- matches
# this project's own "UTR<12 digits>" convention as well as generic alphanumeric
# reference numbers, without requiring the literal substring "UTR".
UTR_LIKE_RE = re.compile(r"\b[A-Za-z]{0,6}\d{6,}\b")


@dataclass
class VerificationResult:
    is_settlement_credit: bool
    confidence_note: str
    source: str  # "ai_verified" | "fallback"


def _keyword_check(description: str) -> bool:
    """The deterministic ground truth this module cross-checks the AI against --
    plain string matching, no AI involved. Requires ALL three signals named in
    the system prompt: RAZORPAY, SETTLEMENT, and a UTR-like reference token."""
    text = description or ""
    has_razorpay = "razorpay" in text.lower()
    has_settlement = "settlement" in text.lower()
    has_utr_like = bool(UTR_LIKE_RE.search(text))
    return has_razorpay and has_settlement and has_utr_like


def _fallback_note(deterministic_result: bool, reason: str) -> str:
    verdict = "consistent with" if deterministic_result else "not consistent with"
    return (
        f"{reason} Deterministic keyword check: narration is {verdict} a genuine "
        f"Razorpay settlement credit (checked for RAZORPAY, SETTLEMENT, and a "
        f"UTR-like reference)."
    )


def _build_prompt(description: str, amount: Any, date: Any) -> str:
    return (
        f'Bank transaction narration: "{description}"\n'
        f"Amount: Rs.{amount}\n"
        f"Date: {date}\n\n"
        "Is this narration consistent with a genuine Razorpay settlement credit?"
    )


def verify_narration(bank_transaction: Dict[str, Any]) -> VerificationResult:
    """
    bank_transaction: {"description": str, "amount": float, "date": str} -- nothing
    else is read here, and no database access happens in this module at all.
    """
    description = bank_transaction.get("description") or ""
    amount = bank_transaction.get("amount")
    date = bank_transaction.get("date")

    deterministic_result = _keyword_check(description)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("path=fallback reason=no_api_key")
        return VerificationResult(
            is_settlement_credit=deterministic_result,
            confidence_note=_fallback_note(deterministic_result, "AI verification unavailable (no API key)."),
            source="fallback",
        )

    prompt = _build_prompt(description, amount, date)

    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS)
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            # cache_control: same rationale and same measured no-op-at-
            # current-length caveat as app/ai_explain.py's identical comment
            # -- see that file and CLAUDE.md's "Prompt caching" note.
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        stop_reason = response.stop_reason
    except Exception:
        logger.exception("path=fallback reason=api_error")
        return VerificationResult(
            is_settlement_credit=deterministic_result,
            confidence_note=_fallback_note(deterministic_result, "AI verification failed (request error)."),
            source="fallback",
        )

    # Same completion check as ai_explain.py/nl_query.py: a truncated response is
    # rejected outright, even if it happens to still contain a parseable YES/NO.
    if stop_reason is not None and stop_reason != "end_turn":
        logger.warning("path=fallback reason=incomplete_response stop_reason=%s", stop_reason)
        return VerificationResult(
            is_settlement_credit=deterministic_result,
            confidence_note=_fallback_note(deterministic_result, "AI response was incomplete."),
            source="fallback",
        )

    if not text:
        logger.warning("path=fallback reason=empty_response")
        return VerificationResult(
            is_settlement_credit=deterministic_result,
            confidence_note=_fallback_note(deterministic_result, "AI returned an empty response."),
            source="fallback",
        )

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    first_line = lines[0].upper() if lines else ""
    if first_line.startswith("YES"):
        ai_result = True
    elif first_line.startswith("NO"):
        ai_result = False
    else:
        logger.warning("path=fallback reason=unparseable_verdict first_line=%r", lines[0] if lines else "")
        return VerificationResult(
            is_settlement_credit=deterministic_result,
            confidence_note=_fallback_note(
                deterministic_result, "AI response was not in the expected YES/NO format."
            ),
            source="fallback",
        )

    ai_reason = " ".join(lines[1:]).strip()

    # "Don't trust the AI's claim blindly" -- same principle as numeric validation
    # elsewhere, applied to this feature's boolean claim instead of a set of numbers.
    if ai_result != deterministic_result:
        logger.warning(
            "path=fallback reason=ai_deterministic_mismatch ai_result=%s deterministic_result=%s",
            ai_result, deterministic_result,
        )
        return VerificationResult(
            is_settlement_credit=deterministic_result,
            confidence_note=_fallback_note(
                deterministic_result,
                f"AI said {'YES' if ai_result else 'NO'} but that disagreed with the deterministic "
                f"keyword check, so the AI response was discarded.",
            ),
            source="fallback",
        )

    logger.info("path=ai_verified is_settlement_credit=%s", ai_result)
    return VerificationResult(
        is_settlement_credit=ai_result,
        confidence_note=ai_reason or "AI verification agreed with the deterministic keyword check.",
        source="ai_verified",
    )
