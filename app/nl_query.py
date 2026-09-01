"""
Natural-language query layer over the full current reconciliation state (all
batches, all exceptions). Same safety model as app/ai_explain.py: this module
takes no database session and imports nothing from app.models -- it can only
see the plain dict context_data explicitly passed to it. Every number in the
response must be verifiable against that data, or the response is discarded.

Reuses app.ai_explain's provider-agnostic helpers (_validate_response,
_date_component_numbers) rather than duplicating them -- the only things
specific to this feature are the system prompt, mapping context_data to
(allowed_numbers, identifier_strings), and the out-of-scope handling.
"""
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Set

from dotenv import load_dotenv

load_dotenv()

import anthropic

from app.ai_explain import MODEL, REQUEST_TIMEOUT_SECONDS, _date_component_numbers, _validate_response

logger = logging.getLogger("ledgertrail.nl_query")

MAX_TOKENS = 500

SYSTEM_PROMPT = (
    "You answer questions ONLY using the data provided below. You must NEVER "
    "calculate, estimate, or introduce any number not explicitly present in the "
    "data. If the question cannot be answered from this data -- because it asks "
    "about something not covered, requires calculation you're not given the "
    "result of, or is unrelated to settlement reconciliation -- respond exactly: "
    "'I don't have that information in the current data.' Do not guess or "
    "speculate under any circumstances. Write plain prose only, no Markdown "
    "formatting. "
    "If a 'Total unexplained amount' figure is provided in the data below, it has "
    "already been calculated for you and is the authoritative answer to ANY "
    "question asking for a total, sum, or aggregate unexplained amount -- "
    "regardless of how the question is phrased, including phrasings like 'across "
    "all batches' that do not say 'open' explicitly. You must use that provided "
    "figure directly. Never add up individual exception amounts yourself to "
    "produce a different total, even if you believe a differently-scoped total "
    "would also be a reasonable answer. "
    "confidence_score and match_type describe how a settlement was matched to a "
    "bank transaction (based on date proximity), not a statistical confidence "
    "interval. If asked about match confidence, describe it as how closely "
    "dates/amounts aligned, not as a probability or certainty measure."
)

OUT_OF_SCOPE_TEXT = "I don't have that information in the current data."
UNVERIFIABLE_TEXT = (
    "I wasn't able to verify part of that answer, so I can't show it. "
    "Try rephrasing your question or ask about a specific batch."
)


@dataclass
class QueryResult:
    text: str
    source: str  # "answered" | "unverifiable" | "out_of_scope"


def _collect_context_numbers(context_data: Dict[str, Any]) -> Set[float]:
    allowed = set()

    for batch in context_data.get("batches", []):
        if batch.get("id") is not None:
            allowed.add(float(batch["id"]))
        for field in (
            "total_gross", "total_refunds", "total_fees", "total_tax", "total_net",
            "matched_bank_amount", "confidence_score", "variance",
        ):
            value = batch.get(field)
            if value is not None:
                allowed.add(round(float(value), 2))
        if batch.get("settlement_date"):
            allowed |= _date_component_numbers(batch["settlement_date"])

    for exc in context_data.get("exceptions", []):
        if exc.get("unexplained_amount") is not None:
            allowed.add(round(float(exc["unexplained_amount"]), 2))

    if context_data.get("total_unexplained_amount") is not None:
        allowed.add(round(float(context_data["total_unexplained_amount"]), 2))

    return allowed


def _build_prompt(question: str, context_data: Dict[str, Any]) -> str:
    lines = ["Current settlement reconciliation data:", "", "Batches:"]

    batches = context_data.get("batches", [])
    if not batches:
        lines.append("- (none)")
    for batch in batches:
        lines.append(
            f"- Batch {batch.get('id')} (settlement_date={batch.get('settlement_date')}): "
            f"total_gross=Rs.{batch.get('total_gross')}, total_refunds=Rs.{batch.get('total_refunds')}, "
            f"total_fees=Rs.{batch.get('total_fees')}, total_tax=Rs.{batch.get('total_tax')}, "
            f"total_net=Rs.{batch.get('total_net')}, matched_bank_amount=Rs.{batch.get('matched_bank_amount')}, "
            f"match_type={batch.get('match_type')}, confidence_score={batch.get('confidence_score')}, "
            f"is_reconciled={batch.get('is_reconciled')}, variance=Rs.{batch.get('variance')}"
        )

    lines.append("")
    lines.append("Exceptions:")
    exceptions = context_data.get("exceptions", [])
    if not exceptions:
        lines.append("- (none)")
    for exc in exceptions:
        lines.append(
            f"- Batch {exc.get('batch_id')}: classification={exc.get('classification')}, "
            f"unexplained_amount=Rs.{exc.get('unexplained_amount')}, status={exc.get('status')}, "
            f"suggested_action={exc.get('suggested_action')}"
        )

    if context_data.get("total_unexplained_amount") is not None:
        lines.append("")
        lines.append(
            f"Total unexplained amount across all currently-open exceptions "
            f"(already calculated, do not recompute it yourself): "
            f"Rs.{context_data['total_unexplained_amount']}"
        )

    lines.append("")
    lines.append(f"Question: {question}")
    return "\n".join(lines)


def answer_query(question: str, context_data: Dict[str, Any]) -> QueryResult:
    """
    context_data: {"batches": [...], "exceptions": [...], "total_unexplained_amount": float}
    -- the full current state, not scoped to one batch. Each batch dict is a
    BatchSummary-shaped dict; each exception dict has batch_id, classification,
    unexplained_amount, status, suggested_action. total_unexplained_amount is
    optional and, when present, must be precomputed by the caller in plain Python
    (sum of unexplained_amount across open exceptions) -- never derived here.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("path=unverifiable reason=no_api_key")
        return QueryResult(text=UNVERIFIABLE_TEXT, source="unverifiable")

    allowed_numbers = _collect_context_numbers(context_data)
    prompt = _build_prompt(question, context_data)

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
        logger.exception("path=unverifiable reason=api_error")
        return QueryResult(text=UNVERIFIABLE_TEXT, source="unverifiable")

    # Same completion check as generate_explanation(): a truncated response with
    # no numbers to flag would otherwise pass the numeric check by accident.
    if stop_reason is not None and stop_reason != "end_turn":
        logger.warning("path=unverifiable reason=incomplete_response stop_reason=%s", stop_reason)
        return QueryResult(text=UNVERIFIABLE_TEXT, source="unverifiable")

    if not text:
        logger.warning("path=unverifiable reason=empty_response")
        return QueryResult(text=UNVERIFIABLE_TEXT, source="unverifiable")

    # The model is instructed to respond with this exact string when the question
    # is out of scope, but in practice it sometimes prepends it and then adds an
    # explanatory sentence rather than replying with only that string. Detect via
    # startswith rather than exact equality so that a correct refusal isn't
    # misclassified as "answered" just because it's not verbatim.
    is_out_of_scope = text.startswith(OUT_OF_SCOPE_TEXT)

    # Numeric validation always runs, even for an out-of-scope-shaped response --
    # an appended explanation could still smuggle in an ungrounded number, so the
    # safety check must not be skipped just because the response starts correctly.
    # No identifier strings to strip here: batch/exception summaries carry no long
    # unique strings (order refs, bank references) the way evidence rows do.
    is_valid, bad_number = _validate_response(text, allowed_numbers, identifier_strings=set())
    if not is_valid:
        logger.warning("path=unverifiable reason=unverifiable_number offending_number=%s", bad_number)
        return QueryResult(text=UNVERIFIABLE_TEXT, source="unverifiable")

    if is_out_of_scope:
        logger.info("path=out_of_scope")
        return QueryResult(text=text, source="out_of_scope")

    logger.info("path=answered")
    return QueryResult(text=text, source="answered")
