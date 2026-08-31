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
Group 1 polish complete (match_basis labeling, simulated operator identity with
fixed dropdown of 3 demo users, dashboard stats card, SQLite/Postgres footer note).

Group 2 complete: AI narration verification feature built and verified. Bank
transaction narrations now follow the REAL Razorpay settlement credit format
(researched, not guessed: "NEFT CR: HDFC BANK [UTR] RAZORPAY SETTLEMENT" -- real
narrations do NOT contain a batch ID, unlike our original assumption). Added 3
unrelated noise bank transactions. AI verification correctly distinguishes all
10 real settlement credits from all 3 noise transactions (13/13 correct, all via
genuine ai_verified agreement with a deterministic keyword cross-check, never
needed fallback). New "Data Sources" panel and "Bank Statement" page both live
and verified. This directly answers the "AI is secondary to the product" critique.

Still open from the improvement list: float->paise currency migration (flagged
high-risk given fully deployed system, needs explicit decision), Razorpay data
CONTRACT/adapter format research (partially done via narration research),
concurrency compare-and-set on approve, hidden holdout dataset. Full auth
explicitly decided against -- fixed dropdown chosen instead.

Next: decide remaining scope given time left, then full regression pass,
redeploy, re-verify live, then rehearsal.

## How to run (when asked)
- API: `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload`
- UI: `cd frontend && npm run dev` → http://localhost:5173/
- Reseed: `python scripts/ingest.py` then `python scripts/run_reconciliation.py`
  (destroys demo staging — re-approve Missing Refund as Sneha afterward)


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

## Operational note: demo staging is destroyed by every regen
ApprovalLog is correctly wiped on every generate_synthetic_data.py/ingest.py run
(necessary — it FK-references ExceptionRecord IDs, which are recreated each regen).
This means: after ANY future regeneration, the demo staging (Missing Refund Record
approved by "Sneha" as the "before" example) is gone and must be redone manually:
POST /exceptions/1/approve {"approver": "Sneha", "decision": "approved"}.
Check this explicitly before any rehearsal or demo, don't assume it's still staged.

AuditEvent deletion bug (New Day 7): both scripts previously called
db.query(models.AuditEvent).delete() during regen, violating the append-only
guarantee. Fixed and verified with a canary-row survival test. Confirmed via grep
(New Day 7 regression check) that no delete/update call touches AuditEvent anywhere
in app/ or scripts/ as of now. This fix means the ONE real historical gap in the
audit trail (an untraceable early approval, exact circumstances unknown) cannot
recur — but that gap itself is real and permanent in the history, not hidden.

## Phase C critical finding (do not regress this)
The evidence-laundering bug (bug 4) is the single most important thing found in this
project. Any future tool added to investigation_tools.py that echoes back an input
value must be excluded from evidence-harvesting for that value, or the verifier can
be tricked into certifying AI-computed arithmetic as "grounded." Check this
explicitly for any NEW comparison/verification tool added in future phases.