import { useState } from 'react'
import { getHeldOutEvaluation } from '../api'
import { formatClassification } from '../lib/format'

const OUTCOME_STYLES = {
  true_positive: 'border-forest/40 bg-forest-wash text-forest',
  true_negative: 'border-rule bg-paper text-ink-muted',
  false_positive: 'border-rust/40 bg-rust-wash text-rust',
  false_negative: 'border-rust/40 bg-rust-wash text-rust',
  ambiguous: 'border-amber/40 bg-amber-wash text-amber',
}

const OUTCOME_LABEL = {
  true_positive: 'Correctly detected',
  true_negative: 'Correctly left clean',
  false_positive: 'False positive',
  false_negative: 'Missed',
  ambiguous: 'Ambiguous — unresolved',
}

function formatPercent(x) {
  if (x === null || x === undefined) return '—'
  return `${(x * 100).toFixed(1)}%`
}

export default function HeldOutEvaluation() {
  const [result, setResult] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  async function run() {
    setLoading(true)
    setError(null)
    try {
      const data = await getHeldOutEvaluation()
      setResult(data)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const m = result?.metrics

  return (
    <section className="mt-10 border-2 border-dashed border-brass/60 bg-paper-raised px-6 py-8">
      <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-brass">Held-out Evaluation</p>
      <h2 className="mt-1 font-serif text-2xl tracking-tight text-ink">
        Run the engine against data it was never tuned to
      </h2>
      <p className="mt-2 max-w-2xl text-sm text-ink-muted">
        A separate, hand-authored set of reconciliation cases (clean matches, timing shifts, fee
        and refund mismatches, an unmatched batch, a duplicate, an ambiguous pair) run through the
        exact same matching/bridge/exception engine used everywhere else on this page — nothing
        simplified or re-implemented for this button.
      </p>

      {!result && !loading ? (
        <button
          type="button"
          onClick={run}
          className="mt-4 rounded-sm border border-ink/30 bg-ink px-4 py-2 text-sm font-medium text-paper-raised hover:bg-ink/90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-forest"
        >
          Run unseen evaluation
        </button>
      ) : null}

      {loading ? <p className="mt-4 text-sm text-ink-muted">Running the held-out dataset through the real engine…</p> : null}

      {error ? (
        <p className="mt-4 text-sm text-rust">
          Could not run the held-out evaluation.
          <button type="button" onClick={run} className="ml-2 underline">
            Try again
          </button>
        </p>
      ) : null}

      {result ? (
        <div className="mt-6">
          <p className="border border-dashed border-brass/70 bg-amber-wash px-3 py-2 text-xs leading-snug text-ink">
            <span className="font-semibold text-brass">Synthetic held-out evaluation</span>
            {' — '}
            {result.dataset_note} This does not prove production accuracy.
          </p>

          <div className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Metric label="Records evaluated" value={m.records_evaluated} />
            <Metric label="Planted exceptions" value={m.planted_exceptions} />
            <Metric label="Detected" value={m.detected_exceptions} />
            <Metric label="Precision" value={formatPercent(m.precision)} />
            <Metric label="Recall" value={formatPercent(m.recall)} />
            <Metric label="True positives" value={m.true_positives} />
            <Metric label="False positives" value={m.false_positives} highlight={m.false_positives > 0} />
            <Metric label="False negatives" value={m.false_negatives} highlight={m.false_negatives > 0} />
            <Metric label="Unresolved / ambiguous" value={m.unresolved_ambiguous_cases} />
            <Metric
              label="Unsafe auto-resolutions"
              value={m.unsafe_auto_resolutions}
              highlight={m.unsafe_auto_resolutions > 0}
            />
            <Metric label="Runtime" value={`${m.runtime_seconds}s`} />
          </div>

          {m.unsafe_auto_resolutions > 0 ? (
            <p className="mt-3 border-2 border-rust bg-rust-wash px-3 py-2 text-sm text-rust">
              {m.unsafe_auto_resolutions} case{m.unsafe_auto_resolutions === 1 ? '' : 's'} where a real
              planted issue was silently left reconciled with no human review required — reported
              here, not hidden. See the case notes below for exactly which one and why.
            </p>
          ) : null}

          <div className="mt-5 overflow-x-auto border border-rule bg-paper">
            <table className="w-full min-w-[820px] text-left text-sm">
              <thead>
                <tr className="border-b border-rule text-[11px] uppercase tracking-[0.14em] text-ink-muted">
                  <th className="px-4 py-2 font-medium">Case</th>
                  <th className="px-4 py-2 font-medium">Type</th>
                  <th className="px-4 py-2 font-medium">Expected</th>
                  <th className="px-4 py-2 font-medium">Detected</th>
                  <th className="px-4 py-2 font-medium">Outcome</th>
                </tr>
              </thead>
              <tbody>
                {result.cases.map((c) => (
                  <tr key={c.batch_label} className="border-b border-rule/70 last:border-0 align-top">
                    <td className="px-4 py-3 font-mono text-xs">{c.batch_label}</td>
                    <td className="px-4 py-3 text-ink-muted">{c.case_type.replace(/_/g, ' ')}</td>
                    <td className="px-4 py-3 font-mono text-xs">
                      {c.expected_classification ? formatClassification(c.expected_classification) : '—'}
                    </td>
                    <td className="px-4 py-3 font-mono text-xs">
                      {c.detected_classification ? formatClassification(c.detected_classification) : '—'}
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`inline-block border px-2 py-0.5 text-[11px] font-semibold ${OUTCOME_STYLES[c.outcome] ?? ''}`}
                      >
                        {OUTCOME_LABEL[c.outcome] ?? c.outcome}
                      </span>
                      {c.note ? <p className="mt-1 max-w-md text-xs leading-snug text-ink-muted">{c.note}</p> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </section>
  )
}

function Metric({ label, value, highlight = false }) {
  return (
    <div className={`border px-3 py-2 ${highlight ? 'border-rust/40 bg-rust-wash' : 'border-rule bg-paper'}`}>
      <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-ink-muted">{label}</p>
      <p className={`mt-0.5 font-serif text-2xl leading-none ${highlight ? 'text-rust' : 'text-ink'}`}>{value}</p>
    </div>
  )
}
