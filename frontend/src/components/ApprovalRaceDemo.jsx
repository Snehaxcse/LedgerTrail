import { useState } from 'react'
import { approveHoldoutSandboxException, startHoldoutApprovalSandbox } from '../api'
import { formatClassification } from '../lib/format'

function parseApiErrorDetail(err) {
  try {
    const parsed = JSON.parse(err.message)
    return parsed?.detail || err.message
  } catch {
    return err.message
  }
}

export default function ApprovalRaceDemo() {
  const [scenario, setScenario] = useState('duplicate_approval')
  const [running, setRunning] = useState(false)
  const [steps, setSteps] = useState({})
  const [error, setError] = useState(null)

  async function run() {
    setRunning(true)
    setSteps({})
    setError(null)
    try {
      const start = await startHoldoutApprovalSandbox()
      if (!start.exception) {
        throw new Error('No exception requiring approval was found in this sandbox run.')
      }
      setSteps((s) => ({ ...s, exception: start.exception }))

      const snehaResponse = await approveHoldoutSandboxException(start.sandbox_id, {
        exceptionId: start.exception.id,
        approver: 'Sneha',
        decision: 'approved',
      })
      setSteps((s) => ({ ...s, sneha: snehaResponse }))

      try {
        await approveHoldoutSandboxException(start.sandbox_id, {
          exceptionId: start.exception.id,
          approver: 'Rahul',
          decision: 'approved',
        })
        setError('Unexpected: the second approval succeeded — this should have been rejected.')
      } catch (err) {
        setSteps((s) => ({
          ...s,
          rahulStatus: err.status,
          rahulDetail: parseApiErrorDetail(err),
        }))
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setRunning(false)
    }
  }

  const started = running || Object.keys(steps).length > 0

  return (
    <section className="mt-8 border border-rule bg-paper-raised px-6 py-6">
      <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-ink-muted">Simulate</p>
      <h3 className="mt-1 font-serif text-xl tracking-tight text-ink">Duplicate approval attempt</h3>
      <p className="mt-2 max-w-2xl text-sm text-ink-muted">
        Approves a throwaway exception in an isolated sandbox as Sneha, then attempts to approve the
        SAME exception again as Rahul — the real compare-and-set endpoint logic
        (_approve_exception_core), the real 409, not a scripted response.
      </p>

      <label className="mt-4 flex max-w-xs flex-col gap-1 text-sm">
        <span className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-muted">
          Scenario
        </span>
        <select
          value={scenario}
          onChange={(event) => setScenario(event.target.value)}
          disabled={running}
          className="rounded-sm border border-rule bg-paper px-3 py-1.5 text-ink outline-none focus:border-forest"
        >
          <option value="duplicate_approval">Duplicate approval (Sneha, then Rahul)</option>
        </select>
      </label>

      {!started ? (
        <button
          type="button"
          onClick={run}
          className="mt-4 rounded-sm border border-rule px-4 py-2 text-sm text-ink hover:border-ink/40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-forest"
        >
          Run simulation
        </button>
      ) : null}

      {error ? (
        <p className="mt-4 text-sm text-rust">
          {error}
          <button type="button" onClick={run} className="ml-2 underline">
            Try again
          </button>
        </p>
      ) : null}

      {steps.exception ? (
        <div className="mt-5 space-y-3 text-sm">
          <div className="border border-rule bg-paper px-3 py-2">
            <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-muted">
              Sandbox exception
            </p>
            <p className="mt-0.5 text-ink">
              {formatClassification(steps.exception.classification)} — ₹
              {Number(steps.exception.unexplained_amount).toFixed(2)}
            </p>
          </div>

          {steps.sneha ? (
            <div className="border border-forest/40 bg-forest-wash px-3 py-2 text-forest">
              <p className="text-[11px] font-semibold uppercase tracking-[0.14em]">✓ Sneha approves</p>
              <p className="mt-0.5 text-ink">
                200 OK — status set to “{steps.sneha.status}” (approval_log_id {steps.sneha.approval_log_id})
              </p>
            </div>
          ) : running ? (
            <p className="text-ink-muted">Approving as Sneha…</p>
          ) : null}

          {steps.rahulDetail ? (
            <div className="border-2 border-rust bg-rust-wash px-3 py-2 text-rust">
              <p className="text-[11px] font-semibold uppercase tracking-[0.14em]">
                ✕ Rahul attempts to approve — {steps.rahulStatus}
              </p>
              <p className="mt-1 text-ink">{steps.rahulDetail}</p>
            </div>
          ) : steps.sneha && running ? (
            <p className="text-ink-muted">Attempting to approve as Rahul…</p>
          ) : null}
        </div>
      ) : null}

      {started && !running ? (
        <button type="button" onClick={run} className="mt-4 block text-xs text-ink-muted underline">
          Run again
        </button>
      ) : null}
    </section>
  )
}
