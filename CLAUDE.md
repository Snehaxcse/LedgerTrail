# LedgerTrail — Razorpay Buildathon Project

## Architecture rule (non-negotiable)
AI NEVER performs or touches arithmetic anywhere in this codebase.
All matching, bridge calculation, and exception detection is deterministic Python.
AI is only used for: explaining exception packets in plain language,
NL query over precomputed facts, drafting approval-brief narrative text.

## Tech stack
Backend: Python + FastAPI + SQLAlchemy + SQLite
Frontend: React + Tailwind (built separately in Cursor — API contract only)

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

**AuditEvent**
- id (PK)
- timestamp, actor ("system" | "AI" | "human"), action
- before_state, after_state (Text, JSON-encoded snapshots)

## Key relationships to remember
- Settlement is bundled: one SettlementBatch = many SettlementEntry rows = one (matched) BankTransaction
- The gross-to-net bridge is computed at the BATCH level (sum of entries), then compared to the batch's matched bank transaction
- "Wrong fee tier" exception = OrderRecord.fee_amount (expected) vs SettlementEntry.fee (actual) mismatch

## Current phase
Day 1 complete and verified: 8-table schema finalized (SettlementBatch added,
single-sided unique FK to BankTransaction, OrderRecord.fee_amount added,
ExceptionRecord renamed). Synthetic dataset generated: 3 batches, ~50-60 order-level
entries, 4 injected error types (missing refund, wrong fee tier, duplicate entry,
timing mismatch), one fully clean batch. Ingestion from raw CSV verified against
a fresh empty database, with duplicate-entry preserved (not deduplicated).
ground_truth.json confirmed accurate.

Next: Day 2 — deterministic matching engine (settlement batch ↔ bank transaction,
by amount/date window/reference) and gross-to-net bridge calculation
(Gross − Refunds − Fees − Tax = Net, compared against matched bank transaction).
No AI/LLM calls anywhere in this logic — pure deterministic Python.