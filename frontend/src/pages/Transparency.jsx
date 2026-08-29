import { useEffect, useState } from 'react'
import { getTransparency } from '../api'
import { formatClassification, formatInr } from '../lib/format'

function formatValue(value) {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'number') return formatInr(value)
  return String(value)
}

function formatBatch(id) {
  return `Batch ${String(id).padStart(2, '0')}`
}

export default function Transparency() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    getTransparency()
      .then((payload) => {
        if (!cancelled) setData(payload)
      })
      .catch((err) => {
        if (!cancelled) setError(err.message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const summary = data?.summary
  const planted = data?.planted_errors ?? []
  const falsePositives = summary?.false_positives
  const hasFalsePositives = Number(falsePositives) > 0

  return (
    <div>
      <p className="text-[11px] font-semibold uppercase tracking-[0.22em] text-brass">Accuracy</p>
      <h1 className="mt-1 font-serif text-4xl tracking-tight text-ink">Transparency</h1>
      <p className="mt-2 max-w-2xl text-ink-muted">
        Planted errors from the synthetic dataset compared with exceptions the engine recorded.
      </p>

      {loading ? <p className="mt-8 text-ink-muted">Loading transparency report…</p> : null}
      {error ? (
        <div className="mt-6 rounded-sm border border-rust/40 bg-rust-wash px-4 py-3 text-rust">
          Could not load the accuracy report.
          <p className="mt-1 font-mono text-xs opacity-80">{error}</p>
        </div>
      ) : null}

      {summary ? (
        <section className="mt-8 border border-ink/20 bg-paper-raised px-6 py-8">
          <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-ink-muted">
            Injected errors detected
          </p>
          <p className="mt-2 font-serif text-6xl leading-none tracking-tight text-ink sm:text-7xl">
            {summary.total_detected} / {summary.total_planted}
          </p>
          <p className="mt-3 text-lg text-ink">
            {summary.total_detected} / {summary.total_planted} injected errors detected
          </p>
          {summary.total_detected === summary.total_planted ? (
            <p className="mt-1 text-sm text-ink-muted">
              Every planted error was caught and correctly classified.
            </p>
          ) : (
            <p className="mt-1 text-sm text-ink-muted">Not every planted error was detected.</p>
          )}
        </section>
      ) : null}

      {summary ? (
        hasFalsePositives ? (
          <section className="mt-4 border-2 border-rust bg-rust-wash px-6 py-6 text-rust">
            <p className="text-[11px] font-semibold uppercase tracking-[0.2em]">False positives</p>
            <p className="mt-2 font-serif text-5xl leading-none">{falsePositives}</p>
            <p className="mt-3 text-base">
              {falsePositives} false positives — classifications the engine raised that are not in the
              planted ground truth.
            </p>
          </section>
        ) : (
          <section className="mt-4 border border-rule bg-paper-raised px-6 py-4">
            <p className="text-sm text-ink">
              False positives: <span className="font-mono font-semibold">{falsePositives}</span>
            </p>
            <p className="mt-1 text-sm text-ink-muted">
              No extra classifications beyond the planted ground-truth set.
            </p>
          </section>
        )
      ) : null}

      {planted.length ? (
        <section className="mt-8">
          <h2 className="text-[11px] font-semibold uppercase tracking-[0.2em] text-ink-muted">
            Planted errors
          </h2>
          <div className="mt-3 overflow-x-auto border border-rule bg-paper-raised">
            <table className="w-full min-w-[720px] text-left text-sm">
              <thead>
                <tr className="border-b border-rule text-[11px] uppercase tracking-[0.14em] text-ink-muted">
                  <th className="px-4 py-2 font-medium">Detected</th>
                  <th className="px-4 py-2 font-medium">Type</th>
                  <th className="px-4 py-2 font-medium">Batch</th>
                  <th className="px-4 py-2 font-medium">Expected</th>
                  <th className="px-4 py-2 font-medium">Actual</th>
                </tr>
              </thead>
              <tbody>
                {planted.map((row, i) => (
                  <tr key={`${row.batch_id}-${row.type}-${i}`} className="border-b border-rule/70 last:border-0">
                    <td className="px-4 py-3">
                      {row.detected ? (
                        <span className="font-semibold text-forest">Yes ✓</span>
                      ) : (
                        <span className="font-semibold text-rust">No — not detected</span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <p className="text-ink">{formatClassification(row.type)}</p>
                      {row.order_ref ? (
                        <p className="mt-0.5 font-mono text-xs text-ink-muted">{row.order_ref}</p>
                      ) : null}
                    </td>
                    <td className="px-4 py-3 font-mono">{formatBatch(row.batch_id)}</td>
                    <td className="px-4 py-3 font-mono">{formatValue(row.expected_value)}</td>
                    <td className="px-4 py-3 font-mono">{formatValue(row.actual_value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}
    </div>
  )
}
