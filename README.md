# LedgerTrail

### AI-assisted settlement reconciliation that does not trust the AI with financial truth.

> **AI investigates. Deterministic systems decide. Humans retain control.**

A settlement can match the bank exactly and still be unreconciled — because individual refunds, fees, or ledger records remain inconsistent underneath a number that happens to add up.

LedgerTrail processes **172 settlement entries across 10 batches**, deterministically reconciles financial state, investigates unresolved exceptions with a read-only AI agent, independently verifies the agent's claims against the evidence it actually retrieved, and applies deterministic policy before any exception can be resolved.

---

## 🚀 Live Demo

- **Live application:** [https://ledger-trail-rho.vercel.app/](https://ledger-trail-rho.vercel.app/)
- **Backend API:** [https://ledgertrail-1.onrender.com](https://ledgertrail-1.onrender.com)
- **Buildathon track:** Track 4 — AI Finance Controller
- **Dataset:** 172 settlement entries / 10 batches
- **Authentication:** None required. Approver identities shown in the demo are simulated and explicitly disclosed in the UI.

### 60-second judge path

1. **Dashboard** — see 172 entries across 10 batches.
2. **Batch 2** — see why an exact bank match can still be unreconciled.
3. **Batch 9** — run the AI investigation and watch an unsupported AI claim get rejected.
4. **Transparency** — inspect benchmark, held-out evaluation and adversarial safeguards.
5. **Audit Trail** — see the human-authorized resolution recorded with before/after state.

---

## The Problem

Finance teams reconciling payment settlements typically have three versions of the truth:

- the merchant's order records
- the payment processor's settlement report
- what actually landed in the bank

A settlement can therefore **match at the bank level while still being wrong underneath**.

Examples include:

- a refund missing from the internal ledger
- a fee-tier mismatch
- a duplicated settlement line
- an expected timing difference
- an unmatched batch
- unexplained settlement variance
- systemic fee drift

Most naive reconciliation systems stop at:

> **"Does the total match?"**

LedgerTrail asks the more useful question:

> **"What explains the discrepancy underneath the total?"**

---

## What Makes LedgerTrail Different?

Most reconciliation systems determine whether totals match.

LedgerTrail separates **financial truth** from **AI interpretation**.

```text
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

The core design principle is:

> The AI can investigate financial ambiguity, but it cannot become the source of financial truth.

---

## Track 4 Alignment


| Track requirement     | LedgerTrail                                                                                                                     |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| 50+ synthetic records | 172 settlement entries across 10 batches                                                                                        |
| Reconciliation        | Deterministic settlement ↔ bank matching and gross-to-net bridge                                                                |
| Exception handling    | Missing refunds, fee mismatches, duplicates, timing differences, unmatched batches, unexplained variance and systemic fee drift |
| AI assistance         | Bounded, read-only, multi-tool investigation agent                                                                              |
| Accuracy measurement  | Synthetic ground-truth regression benchmark + separate held-out evaluation                                                      |
| Human oversight       | Policy-gated resolution with an explicitly identified human approver                                                            |
| Auditability          | Audit trail records before/after state for financial mutations                                                                  |
| Safe automation       | Deterministic policy engine; 0 unsafe auto-resolutions                                                                          |


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
    Matched       Exception
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
       Policy-eligible     Human Review
              │               │
              └───────┬───────┘
                      ▼
              Human confirms
                (named)
                      │
                      ▼
                 Audit Trail

```

**Critical trust boundary:** the LLM has no write access to financial state. Matching, the gross-to-net bridge, exception classification and anomaly detection are deterministic Python logic. No model call sits inside these financial calculations.

### Trust Boundaries


| Component                     | Trusted to do                              |
| ----------------------------- | ------------------------------------------ |
| Reconciliation engine         | Calculate authoritative financial state    |
| Database                      | Persist application state                  |
| AI agent                      | Investigate and form hypotheses            |
| Evidence store / tool results | Provide retrieved application evidence     |
| Claim verifier                | Determine whether AI claims are supported  |
| Policy engine                 | Determine whether a resolution is eligible |
| Human approver                | Authorize the final resolution             |


**The rule:** no probabilistic component is trusted with final financial authority.

---

## What AI Actually Does

The AI is **not** responsible for:

- calculating settlement amounts
- deciding whether bank and settlement totals match
- modifying financial records
- approving or resolving exceptions
- determining authoritative financial truth

Instead, it:

- selects which read-only investigation tools to call
- chooses the order in which to investigate an exception
- retrieves related orders, refunds, bank transactions and settlement evidence
- forms an investigative hypothesis
- produces factual claims tied to the evidence it retrieved
- explains the discrepancy in plain language

**Every financial claim the AI makes is independently checked against the actual evidence returned by the tools it called. The model's confidence is never treated as evidence.**

---

## Why AI, Not Just Deterministic Rules?

Deterministic rules are the right tool for establishing financial truth. They are less useful when an ambiguous exception spans multiple related records and requires investigation.

For example:

```
Settlement exception
       ↓
Order
       ↓
Payment
       ↓
Refund
       ↓
Bank transaction
       ↓
Settlement bridge

```

The AI helps decide what to investigate and how the evidence relates. The verifier and policy engine decide what is actually supported and what the system is allowed to do.

> Rules determine what is true. AI helps determine what to investigate and explain.

---

## Example: When the AI Is Wrong

During development, the investigation agent claimed that a settlement batch contained an exact number of entries it had never actually retrieved from any tool. The real count was different.

LedgerTrail caught it:

```
AI claim
   ↓
Actual retrieved evidence
   ↓
Contradiction detected
   ↓
CLAIM REJECTED
   ↓
Human review

```

The investigation status becomes `CONTRADICTED`. The UI explicitly separates the rejected interpretation from legitimately grounded facts.

**The model can make the claim. It cannot make the claim true.**

This is a real, reproducible failure observed during development — not a staged success case.

---

## Financial Safety Invariants

1. **AI cannot mutate financial state.** Every investigation tool is read-only.
2. **Financial calculations are deterministic.** Settlement bridge calculations and reconciliation decisions never depend on a model call.
3. **AI claims require evidence.** Claims are independently verified against actual tool results before being labeled verified.
4. **Contradictions block trust.** A contradicted claim cannot satisfy resolution policy or be labeled verified.
5. **Policy is enforced server-side.** A client cannot bypass the eligibility gate by sending a trusted-looking flag. The server re-checks policy before honoring a fast-track confirmation.
6. **Approvals are atomic.** A compare-and-set database operation guarantees that only one concurrent request can transition an exception out of `open`.
7. **Replay is rejected.** Duplicate ingestion is blocked by a database-level uniqueness constraint rather than an application-level existence check.
8. **AI failure degrades safely.** Malformed or failed investigations fall back to human review — never to a financial mutation.
9. **Money uses integer arithmetic.** Financial amounts are represented internally as integer paise rather than floating-point values.

---

## Evaluation & Results

**Primary demo dataset** — 172 settlement entries across 10 batches. This is the dataset behind the live product experience.

**Detection benchmark** — 7 planted errors across the primary dataset, covering missing refund, fee tier mismatch, duplicate entry, timing difference, and systemic fee drift. Several exception types are planted more than once, on different orders, to test that detection is not dependent on a single hard-coded example.

**Held-out evaluation** — a separate 14-record dataset, evaluated using the same unmodified reconciliation code inside an isolated database. The held-out dataset is not used to tune the primary reconciliation logic.

### Current measured results


| Metric                                        | Result      |
| --------------------------------------------- | ----------- |
| Primary settlement entries                    | 172         |
| Batches                                       | 10          |
| Primary planted errors detected               | 7 / 7       |
| Primary false positives                       | 0           |
| Held-out records evaluated                    | 14          |
| Held-out planted errors detected              | 7 / 7       |
| Held-out precision / recall                   | 100% / 100% |
| Unsafe auto-resolutions                       | 0           |
| Concurrent duplicate approvals accepted       | 0           |
| Duplicate ingestion events accepted on replay | 0           |


**Important:** the 7/7 and 100%/100% figures are synthetic benchmark results, not production accuracy guarantees. The held-out evaluation exists specifically to distinguish regression testing from a benchmark the system was developed against.

### AI Investigation Reliability

The investigator is probabilistic, so reliability is measured rather than assumed.

A 42-run benchmark across every exception type found:

- **45.2%** completed without requiring human-review fallback
- **54.8%** safely escalated to human review
- **0** cases where a fabricated or contradicted claim was labeled verified

The hardest case — a multi-order investigation — initially escalated 86% of the time and frequently exhausted its tool-call budget. Rather than simply increasing the budget, the failure mode was diagnosed: **the agent was spending calls discovering evidence instead of investigating it.** The tool architecture was redesigned with an upfront case-context tool, explicit stop conditions, and per-case investigation objectives — the tool-call limit was not increased.

After redesign, the same case was re-measured across 10 runs: **86% escalation → 20% escalation**, with zero budget exhaustion.

> **Our investigator is probabilistic. Our financial truth is not.**

---

## Adversarial Testing: What We Broke

A finance system should be tested against failure, not only demonstrated on the happy path.

**Evidence-laundering hole.** The AI once computed a number itself, passed it into a verification tool as an input, and the tool echoed it back. A verifier that only checked whether the value appeared in a tool result could have incorrectly certified it as grounded. *Fix: echoed tool inputs are excluded from evidence that the tool independently confirmed.*

**Approval race condition.** Sequential double-click protection was not enough — two genuinely simultaneous requests could both pass a read-then-write check before either committed. *Fix: atomic compare-and-set database transition, verified with genuinely concurrent requests on a file-based database.*

**Malformed AI output.** Multi-step investigations occasionally produced structurally invalid model output. *Fix: validation → one bounded retry → human-review fallback. No malformed output is silently treated as valid.*

**Currency display bug.** A final re-check of the actual numbers rendered on screen exposed a 100× currency-unit display bug caused by a missed conversion boundary. *Fixed and covered by a regression test.*

A system that never finds anything wrong during its own testing is not necessarily a system that was tested well.

---

## Why Not Fully Automate?

Financial state should not mutate simply because a model is confident. LedgerTrail separates investigation, verification, and resolution authority into three distinct steps. Even when policy finds an investigation's evidence completely clean, a human still selects their identity and confirms the resolution. High-risk or insufficiently evidenced exceptions remain in human review by design.

**Fast-track does not mean AI-authorized.**

---

## Demo Scenario

**Batch 2 — the core reconciliation insight.** The bank amount matches the settlement exactly. Yet the batch remains unreconciled because an unrelated exception is still open. This demonstrates why bank-level agreement is not sufficient evidence of underlying correctness.

**Batch 9 — adversarial AI investigation.** The investigation produces a claim that the retrieved evidence contradicts. The verifier rejects the claim and prevents it from satisfying the resolution policy.

**Agent Demo — multi-tool investigation.** A missing-refund case is investigated across related records using multiple read-only tools. The resulting claims are independently verified before being surfaced as trusted facts.

---

## Prototype Limitations

This is a hackathon prototype, not a production payment system.

- SQLite is used for this deployment. Production would use PostgreSQL or an equivalent transactional store.
- Approver identities are simulated role selections, not authenticated RBAC.
- Settlement, bank and order data are synthetic so accuracy claims remain independently checkable.
- AI investigation reliability is not deterministic and is measured explicitly above.
- Benchmark results do not establish production accuracy.
- The deployed demo uses a preconfigured AI environment; production would require proper secret management, observability and operational controls.

These boundaries are intentional and are not represented as production capabilities.

---

## What We'd Build Next

- PostgreSQL + transactional event store
- Authenticated RBAC / enterprise identity
- Larger longitudinal evaluation set
- Continuous investigation-quality and model monitoring
- Integration with real settlement/reconciliation data feeds

The goal would be to preserve the same trust boundaries while replacing prototype infrastructure with production infrastructure.

---

## Technology Stack


| Layer      | Choice                                  |
| ---------- | --------------------------------------- |
| Frontend   | React / Vite                            |
| Backend    | FastAPI                                 |
| Database   | SQLite — hackathon deployment           |
| AI         | Anthropic Claude Haiku                  |
| Validation | Pydantic + deterministic claim verifier |
| Deployment | Vercel + Render                         |


Money is represented internally as integer paise, not floating-point values, and converted to decimal rupees only at the API response boundary.

---

## Repository Structure

```
app/
  FastAPI backend:
  matching, bridge, exceptions,
  anomaly detection, investigation agent,
  policy engine

scripts/
  synthetic data generation,
  ingestion, held-out data,
  hero-case data

frontend/
  React frontend

tests/
  persisted pytest regression suite

```

---

## The Core Idea

LedgerTrail is not trying to make an LLM the accountant.

It is trying to make an LLM useful to the accountant without allowing the LLM to become the source of financial truth.

**AI investigates. Deterministic systems decide. Humans retain control.**