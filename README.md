# LedgerTrail

**AI-assisted settlement reconciliation that does not trust the AI with financial truth.**

**The problem:** a settlement can match the bank exactly and still be unreconciled — because individual refunds, fees, or ledger records remain inconsistent underneath a number that happens to add up.

LedgerTrail processes 172 settlement entries across 10 batches, deterministically reconciles financial state, investigates unresolved exceptions with a read-only AI agent, independently verifies the agent's claims against the evidence it actually retrieved, and applies deterministic policy before any exception can be resolved.

**AI investigates. Deterministic systems decide. Humans retain control.**

- **Live demo:** https://ledger-trail-rho.vercel.app/
- **Backend:** https://ledgertrail-1.onrender.com
- **Track:** 4 — AI Finance Controller
- **Dataset:** 172 settlement entries / 10 batches

---

## The Problem

Finance teams reconciling payment settlements typically have three versions of the truth: the merchant's own order records, the payment processor's settlement report, and what actually landed in the bank. Small, real discrepancies — a refund the internal system never logged, a fee tier mismatch, a duplicated line item, ordinary settlement-to-bank timing lag — make reconciliation slow and error-prone precisely because a *bank-level* match can look clean while an *underlying* discrepancy sits unexamined.

Most naive tools stop at "does the total match." LedgerTrail's core claim is that this is not sufficient evidence a settlement is correct.

---

## What LedgerTrail Does

```
BANK MATCHES
     ↓
Does NOT necessarily mean
     ↓
BOOKS ARE CORRECT
     ↓
Unresolved exception
     ↓
AI investigates
     ↓
Evidence verification
     ↓
Policy
     ↓
Human resolution
```

**LedgerTrail separates financial truth from AI interpretation.** That is the thesis the rest of this document defends.

---

## Track 4 Alignment

| Requirement | LedgerTrail |
|---|---|
| 50+ synthetic records | 172 settlement entries across 10 batches |
| Reconciliation | Deterministic settlement ↔ bank matching and gross-to-net bridge |
| Exception handling | Missing refund (both directions), fee tier mismatch, duplicate entry, timing difference, unmatched batch, unexplained variance, systemic fee drift |
| AI assistance | Bounded, read-only, multi-tool investigation agent |
| Accuracy measurement | Synthetic ground-truth benchmark (primary dataset) **and** a separate held-out evaluation the engine was never tuned against |
| Human oversight | Policy-gated human resolution — every resolution requires a named, identified human click |
| Auditability | Append-only audit trail, every event with before/after state |
| Safe automation | Deterministic policy engine; 0 unsafe auto-resolutions to date |

---

## Architecture

```
Settlement + Bank + Ledger Data
             │
             ▼
   Deterministic Reconciliation
             │
      ┌──────┴──────┐
      ▼             ▼
   Matched      Exception
                     │
                     ▼
             Read-only AI Agent
                     │
               Tool evidence
                     │
                     ▼
             Claim Verification
                     │
                     ▼
               Policy Engine
                     │
             ┌───────┴───────┐
             ▼               ▼
     Policy-eligible    Human Review
             │               │
             └───────┬───────┘
                      ▼
            Human confirms (named)
                      ▼
                 Audit Trail
```

**The LLM has no write access to financial state.** Matching, the gross-to-net bridge, exception classification, and anomaly detection are plain deterministic Python — no model call sits anywhere inside them.

---

## What AI Actually Does

The AI is **not** responsible for:
- calculating settlement amounts
- deciding whether bank and settlement amounts match
- modifying financial records
- approving or resolving exceptions
- determining authoritative financial truth

Instead, it:
- selects which read-only investigation tools to call, and in what order, per exception
- retrieves related orders, refunds, bank transactions, and settlement evidence
- forms an investigative hypothesis
- produces factual claims tied to the evidence it retrieved
- explains the discrepancy in plain language

**Every financial claim the AI makes is independently checked against the actual evidence returned by the tools it called — never taken on the model's word.**

A deterministic policy engine then decides whether an investigation's evidence is clean enough (zero unverified claims, zero contradictions, non-high severity, zero bank variance) to surface as "eligible for fast-track resolution." Even then, a human still selects their name and clicks to confirm — the policy engine proposes, it never resolves anything on its own.

---

## Example: When the AI Is Wrong

The investigation agent, during real testing, claimed a settlement batch contained an exact number of entries it had never actually retrieved from any tool — it estimated. The real count was different.

```
AI claim
   ↓
Actual retrieved evidence
   ↓
Contradiction detected
   ↓
CLAIM REJECTED
   ↓
Human review — no unsafe resolution
```

The investigation status becomes `CONTRADICTED`, shown explicitly in the UI as **"AI interpretation rejected — authoritative evidence does not support this conclusion,"** with the fabricated claim shown separately from every other, legitimately grounded fact in the same report.

**This is not a staged failure.** It is a real, reproducible mistake the agent made repeatedly during development. LedgerTrail treats an LLM claim as a hypothesis until independently verified — this is that principle caught in the act, not asserted as a feature.

---

## Evaluation & Results

**Primary demo dataset** — 172 settlement entries across 10 batches. This is the dataset behind the live product experience.

**Detection benchmark** — 7 errors deliberately planted across the primary dataset (missing refund, fee tier mismatch, duplicate entry, timing difference, systemic fee drift — several planted more than once, on different orders, to check detection generalizes rather than being tuned to one example).

**Held-out evaluation** — a second, separate 14-record dataset, run through the exact same, unmodified reconciliation code, in an isolated database that never touches the primary dataset. Its purpose is to answer "does this generalize," which a benchmark you also developed against cannot fully answer on its own.

### Current measured results

| Metric | Result |
|---|---|
| Primary settlement entries | 172 |
| Batches | 10 |
| Primary planted errors detected | 7 / 7 |
| Primary false positives | 0 |
| Held-out records evaluated | 14 |
| Held-out planted errors detected | 7 / 7 |
| Held-out precision / recall | 100% / 100% |
| Unsafe auto-resolutions | 0 |
| Concurrent duplicate approvals accepted | 0 (verified with genuinely simultaneous requests, not sequential clicks) |
| Duplicate ingestion events accepted on replay | 0 (rejected by a real database-level uniqueness constraint) |

**The 7/7 result is a regression benchmark against synthetic ground truth, not a claim of production accuracy.** The held-out result exists specifically to make that distinction checkable rather than asserted.

### AI investigation reliability — measured, not assumed

A 42-run benchmark across every exception type found **45.2% of investigations completing autonomously** (a fully or partially verified report) and **54.8% safely escalating to human review** — with **zero** cases where a fabricated or contradicted claim was ever labeled as verified. The benchmark also revealed our hardest case (a multi-order investigation) escalating 86% of the time, frequently exhausting its tool-call budget. Rather than raising that budget, we diagnosed the actual cause — the agent was spending calls discovering evidence rather than investigating it — and redesigned the tool architecture (an upfront case-context tool, a stop condition, per-case investigation objectives) without touching the call limit at all. Re-measured across 10 runs: escalation on that same case dropped from 86% to 20%, with zero budget exhaustion afterward.

**Our investigator is probabilistic. Our financial truth is not.**

---

## Financial Safety Invariants

1. **AI cannot mutate financial state.** Every investigation tool is read-only.
2. **Financial calculations are deterministic.** The settlement bridge and reconciliation decisions never depend on a model call.
3. **AI claims require evidence.** Claims are independently verified against actual tool results before being labeled verified.
4. **Contradictions block trust.** A contradicted claim cannot satisfy resolution policy or be labeled verified.
5. **Policy is enforced server-side.** A client cannot bypass the eligibility gate by sending a trusted-looking flag — the server re-checks eligibility itself before honoring any fast-track confirmation.
6. **Approvals are atomic.** A compare-and-set database operation guarantees only one concurrent request can transition an exception out of "open" — verified with genuinely simultaneous requests, not just sequential clicks.
7. **Replay is rejected.** Duplicate ingestion is blocked by a real database uniqueness constraint, not an application-level existence check that could be bypassed.
8. **AI failure degrades safely.** Malformed or failed investigations fall back to human review — never to a financial mutation.

---

## What We Broke and Fixed

**An evidence-laundering hole.** The AI once computed a number itself — forbidden — passed it into a verification tool as an input, and the tool echoed it back. Our verifier, checking only "did this come from a tool result," would have wrongly certified that self-computed number as grounded evidence. Fixed by excluding a tool's own echoed inputs from what counts as evidence it independently confirmed.

**A race condition in approval.** Sequential double-click protection existed, but two genuinely simultaneous requests could both pass a read-then-write check before either wrote. Fixed with an atomic compare-and-set database operation, verified with real concurrent threads on a file-based database (not `:memory:`, which would have hidden the race by giving each thread its own isolated database).

**Malformed AI tool-call output.** Multi-step investigations occasionally produced structurally invalid output from the model. Fixed with defensive shape validation, a bounded one-time retry, and a safe fallback to human review — never a silent crash or a fabricated result passed through as valid.

**A 100x display bug in the audit trail**, found only in a final, deliberate re-check of the actual numbers rendered on screen (not just that the page loaded). A currency-unit conversion boundary had been missed on one code path. Found before it reached production judging, fixed, and covered by a dedicated regression test.

We expose these because a system that never found anything wrong during its own testing would be a less credible claim than one that found real problems and fixed them.

---

## Why AI, Not Just Deterministic Rules?

Deterministic rules are the right tool for establishing financial truth. They are the wrong tool for investigating an ambiguous exception that spans multiple related records — a settlement exception that requires tracing through an order, a refund, a bank narration, and a bridge calculation to form a coherent explanation. The AI chooses what to investigate and synthesizes the relationships; the verifier and policy engine prevent its interpretation from ever becoming financial truth on its own.

**Rules determine what is true. AI helps determine what to investigate and explain.**

## Why Not Fully Automate?

Financial state should not mutate simply because a model is confident. LedgerTrail separates investigation, verification, and resolution authority into three distinct steps. Even when a policy check finds an investigation's evidence completely clean, resolution still requires a human to select their identity and click to confirm — the same identity requirement as any other approval. High-risk or insufficiently evidenced exceptions stay in human review, by design.

---

## Demo Guide (60 seconds)

1. **Dashboard** — 172 entries, 10 batches, at a glance.
2. **Batch 2** — see why an exact bank match can still be unreconciled.
3. **Batch 9** — run the AI investigation, watch an unsupported claim get rejected.
4. **Transparency** — the primary benchmark, plus the held-out evaluation, live.
5. **Audit trail** — the human resolution and its state transition, permanently recorded.

**Authentication:** none required. Approver identities shown in the demo (Sneha, Rahul, Priya) are explicitly simulated and disclosed in the UI — not production authentication.

---

## Prototype Limitations

- SQLite is used for this deployment; production would use PostgreSQL or an equivalent transactional store.
- Approver identity is a disclosed, simulated role dropdown, not authenticated RBAC.
- Settlement, bank, and order data are synthetic — deliberately, so every accuracy claim here is independently checkable rather than requiring trust in unshareable real data.
- AI investigation reliability is not assumed to be deterministic, and is measured, not asserted, above.
- The 7/7 and 100%/100% figures are benchmark results against synthetic ground truth, not production accuracy guarantees.

These boundaries are intentional and are not represented as production capabilities.

---

## What We'd Build Next

- PostgreSQL and a transactional event store
- Authenticated RBAC / real enterprise identity
- A larger, longitudinal evaluation set beyond the current benchmark and held-out sizes
- Investigation observability and ongoing model-quality monitoring
- Integration with a real settlement/reconciliation data feed

---

## Technology Stack

| Layer | Choice |
|---|---|
| Frontend | React / Vite |
| Backend | FastAPI |
| Database | SQLite (hackathon deployment) |
| AI | Anthropic Claude (Haiku) |
| Validation | Pydantic + a deterministic claim verifier |
| Deployment | Vercel (frontend) + Render (backend) |

Money is represented internally as integer paise, not floating-point values — converted to decimal rupees only at the API response boundary.

---

## Repository Structure

```
app/            FastAPI backend: matching, bridge, exceptions,
                anomaly detection, investigation agent, policy engine
scripts/        Synthetic data generation, ingestion, held-out/hero-case data
frontend/       React frontend
tests/          Persisted pytest regression suite
```
