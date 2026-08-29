# LedgerTrail — Razorpay Buildathon Project

## Architecture rule (non-negotiable)
AI NEVER performs or touches arithmetic anywhere in this codebase.
All matching, bridge calculation, and exception detection is deterministic Python.
AI is only used for: explaining exception packets in plain language,
NL query over precomputed facts (planned), drafting approval-brief narrative text (planned).
Every AI-generated response must be programmatically verified — every number it states
must be checked against the source data before being shown; if unverifiable, discard
and use a safe fallback template instead.

## Tech stack
Backend: Python + FastAPI + SQLAlchemy + SQLite
Frontend: React + Tailwind (built separately in Cursor — API contract only)
In-product AI layer: Anthropic API, Claude Haiku (claude-haiku-4-5-20251001) — used ONLY
for the explanation/NL-query features below. Paid tier ($5 added), no free-tier quota
walls. (Claude Code and Cursor, which write this codebase, are the same underlying
provider but a separate, tooling-level use — not part of the running product's own
API calls.)

## Data model (final — confirmed Day 1)

**SettlementBatch**
- id (PK)
- settlement_date, total_gross, total_refunds, total_fees, total_tax, total_net
- bank_transaction_id (FK → bank_transactions.id, nullable) — single-sided link, set by the matching engine once a batch is matched. BankTransaction does NOT have a reverse column; query via backref "matched_batch" instead.

**SettlementEntry** (order-level rows within a batch)
- id (PK)
- batch_id (FK → settlement_batches.id, NOT NULL)
- order_ref, gross_amount, fee, tax, refund (default 0.0), net_amount
- NOTE: settlement_date lives on the batch, not here — don't duplicate it

**BankTransaction**
- id (PK)
- amount, date, reference, description (nullable)
- NOTE: has no batch_id column — matched via SettlementBatch.bank_transaction_id only

**OrderRecord**
- id (PK)
- order_ref, amount, status
- refund_amount (nullable)
- fee_amount (NOT NULL) — the EXPECTED fee, diffed against SettlementEntry.fee to detect "wrong fee tier" exceptions

**Match**
- id (PK)
- settlement_batch_id (FK), bank_transaction_id (FK)
- confidence_score, match_type ("exact" | "fuzzy")

**ExceptionRecord** (table name stays "exceptions" — class renamed from Exception to avoid shadowing Python's built-in)
- id (PK)
- unexplained_amount, classification, suggested_action (nullable)
- status ("open" | "approved" | "rejected", default "open")
- linked_evidence_ids (Text, JSON-encoded list)

**ApprovalLog**
- id (PK)
- exception_id (FK → exceptions.id)
- approver, decision, timestamp, resulting_action (nullable)
- reason (String, nullable) — required when decision="rejected"; the approve endpoint returns 400 if it is missing or empty

**AuditEvent**
- id (PK)
- timestamp, actor ("system" | "AI" | "human"), action
- before_state, after_state (Text, JSON-encoded snapshots)

## Key relationships to remember
- Settlement is bundled: one SettlementBatch = many SettlementEntry rows = one (matched) BankTransaction
- The gross-to-net bridge is computed at the BATCH level (sum of entries), then compared to the batch's matched bank transaction
- "Wrong fee tier" exception = OrderRecord.fee_amount (expected) vs SettlementEntry.fee (actual) mismatch

## Current phase
Buildathon deadline extended: 15 days total from Aug 29, 2026 (previously 5).
Tier 0 + Tier 1 fully complete and verified — the entire original 5-day scope,
demo-ready. Demo data staged: Missing Refund Record (Batch 2) pre-approved as a
"before" example; Fee Tier Mismatch (Batch 2) and Duplicate Entry (Batch 3) left
open for live demo; Timing Difference (Batch 4) needs no action. App is staged,
not a sandbox — no exploratory clicking without a deliberate reset
(scripts/run_reconciliation.py) afterward.

AI Explanation Layer (New Day 1): app/ai_explain.py, ExceptionRecord.ai_explanation
column, and GET /batches/{id}/exceptions/{id}/explain are built.

PROVIDER HISTORY (so future-you isn't confused by old references): originally built
against Anthropic, briefly switched to Google Gemini free tier, then REVERTED back
to Anthropic (paid, $5 added) after Gemini's free-tier RPD (20/day) and a
default "thinking" mode silently consuming the entire output token budget caused
3 of 4 real exceptions to fail. Anthropic is correct and final as of this note —
if you see any Gemini references elsewhere in this file or the code, they're stale
and should be corrected to Anthropic.

Known fix carried forward regardless of provider: max_output_tokens must be 500,
not 300 — Missing Refund Record's prompt (2 rows x 5 fields) is structurally tight
against 300 tokens. Also: always check finish_reason/stop_reason — a truncated
response with zero numbers can pass the numeric-verification check by accident
(no numbers to flag != trustworthy), so truncation must be caught explicitly.

Next: verify the reverted Anthropic implementation against all 4 real exceptions,
including Missing Refund Record (never successfully got a real AI explanation
during the Gemini attempt due to quota exhaustion).