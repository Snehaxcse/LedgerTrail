import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { getTrend } from '../api'
import { formatDate, formatDateShort } from '../lib/format'

export default function Trend() {
  const [points, setPoints] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    getTrend()
      .then((data) => {
        if (!cancelled) setPoints(data)
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

  const reconciledCount = points.filter((point) => point.is_reconciled).length
  const openCount = points.length - reconciledCount
  const firstDate = points[0]?.settlement_date
  const lastDate = points[points.length - 1]?.settlement_date

  return (
    <div>
      <p className="text-[11px] font-semibold uppercase tracking-[0.22em] text-brass">Trend</p>
      <h1 className="mt-1 font-serif text-4xl tracking-tight text-ink">Over time</h1>
      <p className="mt-2 max-w-xl text-ink-muted">
        Each mark is one settlement, in date order. Green is closed. Amber still has open
        exceptions — not a bank mismatch.
      </p>

      {loading ? <p className="mt-8 text-ink-muted">Loading trend…</p> : null}
      {error ? (
        <div className="mt-6 rounded-sm border border-rust/40 bg-rust-wash px-4 py-3 text-rust">
          Could not load the trend.
          <p className="mt-1 font-mono text-xs opacity-80">{error}</p>
        </div>
      ) : null}

      {!loading && !error && points.length === 0 ? (
        <p className="mt-8 text-ink-muted">No settlements to plot yet.</p>
      ) : null}

      {points.length ? (
        <section className="mt-8 border border-ink/20 bg-paper-raised px-5 py-7 sm:px-8">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <p className="font-serif text-3xl tracking-tight text-ink">
              {reconciledCount} of {points.length} reconciled
            </p>
            <ul className="flex flex-wrap gap-4 text-sm">
              <Legend swatch="bg-forest" label="Reconciled" count={reconciledCount} />
              <Legend swatch="bg-amber" label="Not reconciled" count={openCount} />
            </ul>
          </div>

          <div className="-mx-1 mt-10 overflow-x-auto pb-1">
            <ol className="relative flex min-w-full items-start justify-between gap-2">
              <div
                className="absolute top-[18px] right-4 left-4 h-px bg-rule"
                aria-hidden="true"
              />
              {points.map((point) => {
                const closed = point.is_reconciled
                const label = closed ? 'Reconciled' : 'Not reconciled'
                return (
                  <li
                    key={point.batch_id}
                    className="relative z-10 flex w-14 shrink-0 flex-col items-center"
                  >
                    <Link
                      to={`/batches/${point.batch_id}`}
                      title={`Batch ${String(point.batch_id).padStart(2, '0')} · ${label}`}
                      aria-label={`Batch ${String(point.batch_id).padStart(2, '0')}, ${formatDate(point.settlement_date)}, ${label}`}
                      className="flex flex-col items-center no-underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-forest"
                    >
                      <span
                        className={`block h-9 w-9 shrink-0 rounded-full border-2 border-paper-raised shadow-[0_0_0_1px_rgba(22,20,16,0.08)] ${
                          closed ? 'bg-forest' : 'bg-amber'
                        }`}
                      />
                      <span className="mt-3 font-serif text-lg leading-none text-ink">
                        {String(point.batch_id).padStart(2, '0')}
                      </span>
                      <span className="mt-1 whitespace-nowrap text-[11px] text-ink-muted">
                        {formatDateShort(point.settlement_date)}
                      </span>
                    </Link>
                  </li>
                )
              })}
            </ol>
          </div>

          {firstDate && lastDate ? (
            <div className="mt-6 flex justify-between text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-muted">
              <span>{formatDate(firstDate)}</span>
              <span>{formatDate(lastDate)}</span>
            </div>
          ) : null}
        </section>
      ) : null}
    </div>
  )
}

function Legend({ swatch, label, count }) {
  return (
    <li className="flex items-center gap-2 text-ink-muted">
      <span className={`h-3 w-3 rounded-full ${swatch}`} aria-hidden="true" />
      <span>
        {label}
        <span className="text-ink"> {count}</span>
      </span>
    </li>
  )
}
