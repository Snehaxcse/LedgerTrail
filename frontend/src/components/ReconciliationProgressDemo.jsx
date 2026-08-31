import { useState } from 'react'
import {
  runHoldoutSandboxBridge,
  runHoldoutSandboxClassify,
  runHoldoutSandboxMatch,
  startHoldoutReconciliationSandbox,
} from '../api'

// Each entry's `done`/`label` is derived from a REAL, separately-awaited
// network call's response (see run() below) -- there is no timer or fake
// delay anywhere in this component. The last three rows all read from the
// SAME classify response (duplicates + requires-review are sub-facts of one
// classify_exceptions() call, not separately-run computations), so they
// complete together rather than pretending to be three sequential steps.
function buildSteps(completed) {
  return [
    {
      key: 'load',
      pending: 'Loading settlement records…',
      done: completed.load ? `Loaded ${completed.load.records_loaded} settlement records` : null,
    },
    {
      key: 'match',
      pending: 'Matching bank credits…',
      done: completed.match ? `Matched ${completed.match.matched_count} bank credits` : null,
    },
    {
      key: 'bridge',
      pending: 'Calculating bridges…',
      done: completed.bridge ? `Calculated ${completed.bridge.bridges_calculated} bridges` : null,
    },
    {
      key: 'classify',
      pending: 'Classifying exceptions…',
      done: completed.classify ? `Classified ${completed.classify.total_exceptions} exceptions` : null,
    },
    {
      key: 'duplicates',
      pending: 'Detecting duplicates…',
      done: completed.classify
        ? `Detected ${completed.classify.duplicates_detected} duplicate${completed.classify.duplicates_detected === 1 ? '' : 's'}`
        : null,
    },
    {
      key: 'review',
      pending: 'Identifying cases requiring review…',
      done: completed.classify
        ? `Identified ${completed.classify.requires_review} case${completed.classify.requires_review === 1 ? '' : 's'} requiring review`
        : null,
    },
  ]
}

export default function ReconciliationProgressDemo() {
  const [running, setRunning] = useState(false)
  const [completed, setCompleted] = useState({})
  const [activeKey, setActiveKey] = useState(null)
  const [error, setError] = useState(null)

  async function run() {
    setRunning(true)
    setCompleted({})
    setError(null)
    try {
      setActiveKey('load')
      const start = await startHoldoutReconciliationSandbox()
      setCompleted((c) => ({ ...c, load: start }))

      setActiveKey('match')
      const match = await runHoldoutSandboxMatch(start.sandbox_id)
      setCompleted((c) => ({ ...c, match }))

      setActiveKey('bridge')
      const bridgeResult = await runHoldoutSandboxBridge(start.sandbox_id)
      setCompleted((c) => ({ ...c, bridge: bridgeResult }))

      setActiveKey('classify')
      const classify = await runHoldoutSandboxClassify(start.sandbox_id)
      setCompleted((c) => ({ ...c, classify }))
      setActiveKey(null)
    } catch (err) {
      setError(err.message)
      setActiveKey(null)
    } finally {
      setRunning(false)
    }
  }

  const steps = buildSteps(completed)
  const started = running || Object.keys(completed).length > 0

  return (
    <section className="mt-8 border border-rule bg-paper-raised px-6 py-6">
      <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-ink-muted">Live run</p>
      <h3 className="mt-1 font-serif text-xl tracking-tight text-ink">Run reconciliation</h3>
      <p className="mt-2 max-w-2xl text-sm text-ink-muted">
        Each step below is a real, separately-awaited call into the held-out sandbox's isolated
        database — matching.run_matching, bridge.compute_bridge, exceptions.classify_exceptions, the
        same functions used everywhere else in this app. Nothing here is a timed animation.
      </p>

      {!started ? (
        <button
          type="button"
          onClick={run}
          className="mt-4 rounded-sm border border-rule px-4 py-2 text-sm text-ink hover:border-ink/40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-forest"
        >
          Run reconciliation
        </button>
      ) : null}

      {error ? (
        <p className="mt-4 text-sm text-rust">
          Could not complete the run.
          <span className="ml-2 font-mono text-xs opacity-80">{error}</span>
          <button type="button" onClick={run} className="ml-2 underline">
            Try again
          </button>
        </p>
      ) : null}

      {started ? (
        <ol className="mt-5 space-y-1.5">
          {steps.map((step) => {
            const isDone = Boolean(step.done)
            const isActive = activeKey === step.key
            return (
              <li key={step.key} className="flex items-center gap-2 text-sm">
                <span
                  aria-hidden="true"
                  className={`inline-flex h-4 w-4 shrink-0 items-center justify-center text-xs ${
                    isDone ? 'text-forest' : isActive ? 'text-brass' : 'text-ink-muted/40'
                  }`}
                >
                  {isDone ? '✓' : isActive ? '…' : '○'}
                </span>
                <span className={isDone ? 'text-ink' : isActive ? 'text-ink' : 'text-ink-muted/60'}>
                  {step.done || step.pending}
                </span>
              </li>
            )
          })}
        </ol>
      ) : null}

      {started && !running && !error ? (
        <button type="button" onClick={run} className="mt-4 block text-xs text-ink-muted underline">
          Run again
        </button>
      ) : null}
    </section>
  )
}
