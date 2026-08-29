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
New Day 5 complete: severity weighting added — deterministic rules (classification +
₹1,000 amount threshold) computed and stored at classification time, no impact on
existing is_reconciled/classification logic (re-verified unchanged across all 10
batches). Exception queue UI shows color-coded severity badges (rust=high,
amber=medium, forest=low, gray=info) with All/High/Medium/Low/Info filter toggle,
default sorted high-first. Verified across batches 2, 3, and 4.

Next: New Day 6 - Explainability drill-down (Tier 3). Cheap relative to other
features since GET /batches/{id}/evidence already exists and does most of the work -
this is primarily a frontend task (click a number, see the source rows).


## Operational notes (learned the hard way — don't relearn these)
1. Run uvicorn with --reload during development. Without it, a running server
   keeps executing old code from memory even after files change on disk.
2. ai_explanation is cached per-exception in the database. After any prompt/logic
   change in ai_explain.py, clearing old cached values is a SEPARATE required step
   (UPDATE exceptions SET ai_explanation = NULL) — restarting the server alone does
   NOT clear already-cached database values. Clicking "Generate explanation" on an
   exception that already has a cached value returns the cache, not a fresh call.
3. AI explanation system prompt must explicitly forbid Markdown formatting
   (no #, **, or list markers) — plain prose only, since the frontend displays
   the text as-is with no Markdown rendering.