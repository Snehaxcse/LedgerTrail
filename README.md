# LedgerTrail

A Razorpay-style settlement reconciliation tool: it takes a batch of settlement
entries, bridges them to a bank credit, flags whatever doesn't add up as a
typed exception, and routes every exception through a human approval step
before it's considered resolved. An AI investigation agent can be asked to
gather evidence and propose a hypothesis for an open exception — but only
after a deterministic verifier has checked every number it states against the
actual source data.

Backend: Python + FastAPI + SQLAlchemy + SQLite. Frontend: React + Tailwind
(`frontend/`, deployed separately). AI: Anthropic API, Claude Haiku, used only
for the two features described in [§3](#3-ai-has-read-only-investigative-authority-never-write-access).

This document describes what the system actually does today, not what was
originally planned — see `CLAUDE.md` for phase-by-phase build history if
that's useful, but treat this file as the current source of truth.

## Architecture, in eight points

### 1. Money is integer paise everywhere except the API surface

Every currency column in the database (`SettlementBatch.total_gross`,
`SettlementEntry.gross_amount`, `BankTransaction.amount`, `OrderRecord.amount`,
`ExceptionRecord.unexplained_amount`, etc.) is an `Integer` storing **paise**,
never a float. All matching, bridge, and exception-classification arithmetic
(`app/matching.py`, `app/bridge.py`, `app/exceptions.py`,
`app/anomaly_detection.py`) operates on these integers directly, so amount
comparisons are exact integer equality, not float tolerance-guessing.

Conversion to decimal rupees happens at exactly one boundary:
`app/money.py`'s `paise_to_rupees()`, using `Decimal` division (never
float division), applied only where a response model or AI-prompt string is
actually built — see that module's docstring for the full list of call sites,
including the two AI-prompt-embedding sites that would silently corrupt AI
grounding if missed.

### 2. Financial truth is 100% deterministic

Matching (`app/matching.py`), the gross→net bridge (`app/bridge.py`),
exception classification (`app/exceptions.py`), and cross-batch anomaly
detection (`app/anomaly_detection.py`) are plain Python and SQL. No LLM call
appears anywhere in that call graph. This is the project's non-negotiable
rule (see `CLAUDE.md`'s "Architecture rule") and it has held for the entire
build — every AI feature added since was designed to read the *output* of
this pipeline, never to participate in producing it.

### 3. AI has read-only investigative authority, never write access

The AI investigation agent (`app/investigation_agent.py`) can only call the
ten bounded, read-only tools in `app/investigation_tools.py`:
`get_settlement_batch`, `get_settlement_entries`, `get_bank_candidates`,
`get_order`, `get_refunds`, `get_exception_evidence`, `calculate_bridge`,
`verify_amount_relationship`, `verify_reference_relationship`, and
`verify_narration`. Every one of them returns already-computed data or does a
narrow, deterministic comparison — none of them writes to the database, and
none of them lets the AI perform or influence arithmetic that later gets
treated as ground truth (`verify_amount_relationship`'s own tool description
tells the model exactly this: it can't tell whether its inputs were genuinely
sourced or invented, so that responsibility is entirely the model's — and is
checked afterward, see §4). The agent's output is a hypothesis
(`possible_root_cause`, `recommended_next_step`, a set of claimed
`verified_facts`) that a human reads; it cannot approve, reject, or resolve
anything itself. `calculate_bridge`'s tool description also explicitly warns
the model that `is_reconciled` means only "the bank credit matches," not
"this batch is fully resolved" — the same distinction the dashboard's own
status banner makes.

### 4. Every AI claim is deterministically verified before being labeled "verified"

`_verify_investigation_result()` in `app/investigation_agent.py` is a pure,
network-free function: given the AI's raw structured report and the actual
tool-call log from that investigation, it independently recomputes
`verified_facts` / `unverified_claims` / `contradictions` /
`investigation_status` from scratch. A claim only survives into
`verified_facts` if every number in it matches a number an actual tool call
returned in that same investigation (`_claim_is_grounded`, with unit
tolerance for ratio-vs-percentage phrasing and identifier-string exclusion so
order refs/UTRs aren't mistaken for fabricated numbers). The AI's own
self-reported `investigation_status` is kept only as `ai_self_reported_status`
for transparency — the verifier's own recomputed status is what the API
actually returns, and the two are shown separately in the UI specifically so
a reader can see when the verifier overrode the AI (see
`InvestigationTrace.jsx`).

This was tested against a genuine exploit, not a hypothetical: an earlier
version let the AI compute a number itself (e.g. `baseline_rate × gross`),
pass it through the `verify_amount_relationship` tool, and then cite the
tool's echoed-back result as "grounded evidence" — laundering a self-computed
number through a tool call. Fixed by excluding comparison-utility tools from
evidence-harvesting (`_NON_EVIDENCE_TOOLS`); see `CLAUDE.md`'s "Phase C
critical finding" for why this must never regress.

### 5. Human approval is required, and it's atomic

`POST /exceptions/{id}/approve` is the only way an exception's status
changes. The `open → approved/rejected` transition is a single
compare-and-set `UPDATE ... WHERE status = 'open'`
(`app/main.py::approve_exception`), not a read-then-write — two genuinely
simultaneous requests for the same exception can both pass the initial
validation with a stale in-memory view, but only one `UPDATE` can actually
match `status='open'` in the database; the other necessarily affects 0 rows
and gets a `409`. `tests/test_approve_concurrency.py` proves this with real
threads (a `threading.Barrier` to force genuine concurrency, not sequential
calls): exactly one of two simultaneous approve calls on the same exception
wins, exactly one `ApprovalLog` row is written, and the loser gets a
structured 409, never a torn or double write.

### 6. Held-out evaluation numbers are a synthetic benchmark, not a production accuracy claim

`GET /evaluation/held-out` (surfaced on the Transparency page) runs the real,
unmodified pipeline — `matching.run_matching`, `bridge.compute_bridge`,
`exceptions.classify_exceptions`, `anomaly_detection.run_anomaly_detection`,
imported and called exactly as `app/startup.py` calls them, never a
simplified reimplementation — against a synthetic dataset
(`scripts/generate_holdout_data.py`) inside a fresh, isolated in-memory
SQLite database created on every call. It never imports or touches
`app.database.engine`/`SessionLocal`; the real `ledgertrail.db` is
structurally unreachable from it, not just avoided by convention (see
`tests/test_holdout_evaluation.py`). The metrics it reports (precision,
recall, unsafe-auto-resolution count) describe how the engine performs
against *this planted-case dataset*, separate from the ten batches / seven
exceptions shown everywhere else in the demo (the "primary dataset," whose
own 7/7 classification match is shown on the same Transparency page). Neither
number is a claim about real-world settlement data, which this project has
never had access to — both are explicitly the best available substitute,
labeled as such in the UI.

### 7. The approver dropdown is not authentication

`POST /exceptions/{id}/approve` requires `approver` to be one of three fixed
names in `app/main.py`'s `DEMO_APPROVERS` dict (Sneha, Rahul, Priya) — this
closes the "type literally anything into a free-text field" gap, nothing
more. There is no login, session, or token behind it; picking a name off a
dropdown proves nothing about who is actually calling the endpoint. The
approver name is recorded verbatim into `ApprovalLog` and `AuditEvent` as
this *simulated* identity. The frontend labels this explicitly ("Simulated
operator identity — a fixed list of demo names, not real authentication") —
see `ExceptionQueue.jsx`. Full auth was explicitly considered and decided
against for this project's scope.

### 8. The AI investigation agent has a measured, disclosed reliability limitation

Claude Haiku's tool-call output has a **measured 50–75% per-attempt
malformed-shape rate** on these multi-tool investigations: a list-typed report
field (`verified_facts`, `unverified_claims`, or `contradictions`) is
occasionally serialized as a bare string instead of a JSON array, despite the
tool schema correctly declaring it an array. This was measured directly, not
assumed: a 20-run tally against a real case found ~50% (later corroborated by
independent 20-run tallies at 55% and the hero case at 35%), and a follow-up
diagnostic ruled out "the model loses formatting discipline over a long
tool-use conversation" as the cause — malformed attempts actually averaged
*fewer* tool calls than clean ones (4.18 vs 5.00 in that sample). Several
captured examples show the mechanism: literal leaked fragments like
`<item>...</item>` and a stray `</unverified_claims>` inside the malformed
string, consistent with Claude's internal XML-tagged tool-argument drafting
occasionally failing to convert cleanly into a JSON array. Full writeup:
`app/investigation_agent.py`'s `is_malformed_shape_result` docstring.

**This is never silently absorbed.** When it happens, the deterministic
shape-validation in `_verify_investigation_result` discards the malformed
field entirely and returns `HUMAN_REVIEW_REQUIRED` — never guesses at
parsing it, never lets it corrupt `verified_facts`. The mitigations in place
are disclosed, bounded, and honest about not fixing the underlying rate:

- `investigate_exception()` retries **once** on a malformed first attempt
  (fresh request, no carryover of the bad output) before falling through to
  the existing safety defense. This is investigate_exception's real, live
  behavior for any fresh/uncached call, unconditionally.
- At boot, `app/startup.py`'s `run_investigation_prewarming()` additionally
  pre-runs the two investigations shown in the demo by default (the
  adversarial case and the hero case) up to **5 bounded attempts each**,
  accepting whatever the final attempt produces if none land clean — never
  an unbounded loop, and it logs clearly which attempt succeeded. This
  exists purely so a visitor sees a representative result on first click
  instead of live-rolling the measured failure rate themselves.
- Pre-warming runs as a **background task, after the app has already started
  accepting requests** — not inside the FastAPI startup event. An earlier
  version awaited it synchronously in the startup event, which was measured
  to make the *entire app* (not just the investigation endpoints) unreachable
  for however long pre-warming took, up to several minutes in the worst case
  (confirmed live: `GET /batches` issued while pre-warming was mid-flight got
  no response at all until pre-warming finished). Fixed via
  `starlette.concurrency.run_in_threadpool` + `asyncio.create_task` — see
  `app/main.py`'s `_on_startup` and `app/startup.py`'s module docstring for
  the full account, including the fix being found and applied the same day.
  Until pre-warming finishes, both investigation endpoints simply fall back
  to a live, uncached call — exactly their behavior before pre-warming
  existed.

None of this reduces the underlying ~50–75% per-attempt rate. It reduces how
often a visitor *sees* it, and it never claims otherwise — see
`CLAUDE.md`'s "AI Investigation Agent — known limitation and demo-reliability
measure" for the disclosure this section summarizes.

## Running it

Backend:

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

`--reload` matters during development — without it, a running server keeps
executing old code from memory even after files change on disk.

On every boot, `app/startup.py::run_startup_sequence()` regenerates the demo
dataset from scratch (fixed random seed) and re-runs the full pipeline, so a
fresh deploy always starts from the same known-good state regardless of what
a previous visitor changed. `ANTHROPIC_API_KEY` (see `.env`) is required for
the AI features; if it's absent, they degrade to their documented fallback
behavior rather than failing the whole app.

Frontend:

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173/ — Vite proxies `/api/*` to the FastAPI server. See
`frontend/README.md` for the Vercel deployment variant.

## Testing

```bash
python -m pytest tests/ -q
```

56 tests as of this writing, all persisted (no one-off manual scripts left
standing in for regression coverage). Every isolated-DB test file
(`test_holdout_evaluation.py`, `test_investigation_tools.py`,
`test_investigation_agent.py`, `test_investigation_agent_live.py`,
`test_startup_prewarm.py`, `test_tolerance_and_refund_fixes.py`,
`test_approve_concurrency.py`) either uses the `scratch_db` fixture
(`conftest.py`, a fresh in-memory SQLite session) or builds its own isolated
engine, and never imports `app.database.engine`/`SessionLocal` — the real
`ledgertrail.db` is structurally unreachable from the test suite, not just
avoided by discipline. This has been independently re-verified via
byte-for-byte hash comparison after every phase of this project's build, not
assumed.

## Repo layout

```
app/                    FastAPI app, deterministic pipeline, AI agent
  matching.py             deterministic settlement <-> bank matching
  bridge.py                gross -> net bridge computation
  exceptions.py            deterministic exception classification
  anomaly_detection.py     cross-batch statistical anomaly detection
  money.py                 the one paise -> rupee conversion boundary
  investigation_tools.py   the AI agent's 10 bounded read-only tools
  investigation_agent.py   agent orchestration + the deterministic verifier
  hero_case.py              isolated demo dataset for the "success" showcase
  holdout_evaluation.py    isolated synthetic-benchmark harness
  startup.py                boot-time pipeline + investigation pre-warming
  demo_cache.py             in-memory cache for the ephemeral hero-case demo
  main.py                   FastAPI routes
scripts/                 data generation / ingestion / manual reconciliation
frontend/                React + Tailwind UI (separate README)
tests/                   persisted pytest suite (see Testing above)
CLAUDE.md                phase-by-phase build history and operational notes
```
