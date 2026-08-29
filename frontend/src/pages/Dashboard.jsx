import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { getBatches } from '../api'
import MiniBridge from '../components/MiniBridge'
import StatusBanner from '../components/StatusBanner'
import Amount from '../components/Amount'
import { formatDate } from '../lib/format'

export default function Dashboard() {
  const [batches, setBatches] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    getBatches()
      .then((data) => {
        if (!cancelled) setBatches(data)
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

  const reconciledCount = batches.filter((batch) => batch.is_reconciled).length
  const openCount = batches.filter((batch) => !batch.is_reconciled).length

  return (
    <div>
      <div className="mb-8 flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-[0.22em] text-brass">Batches</p>
          <h1 className="mt-1 font-serif text-4xl tracking-tight text-ink">Settlement dashboard</h1>
          <p className="mt-2 max-w-xl text-ink-muted">
            Status is taken from <span className="font-medium text-ink">is_reconciled</span>. A matching
            bank amount does not turn a batch green.
          </p>
        </div>
        {!loading && !error ? (
          <dl className="flex gap-6 text-sm">
            <Stat label="Batches" value={batches.length} />
            <Stat label="Reconciled" value={reconciledCount} tone="forest" />
            <Stat label="Not reconciled" value={openCount} tone="rust" />
          </dl>
        ) : null}
      </div>

      {loading ? <p className="text-ink-muted">Loading batches…</p> : null}
      {error ? (
        <div className="rounded-sm border border-rust/40 bg-rust-wash px-4 py-3 text-rust">
          Cannot reach the LedgerTrail API. Start FastAPI on port 8000, then refresh.
          <p className="mt-1 font-mono text-xs opacity-80">{error}</p>
        </div>
      ) : null}

      <ul className="space-y-4">
        {batches.map((batch) => (
          <li key={batch.id}>
            <Link
              to={`/batches/${batch.id}`}
              className="block rounded-sm border border-rule bg-paper-raised no-underline shadow-[0_1px_0_rgba(22,20,16,0.04)] transition hover:border-ink/30 hover:shadow-md focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-forest"
            >
              <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-rule px-5 py-3">
                <div className="flex items-baseline gap-3">
                  <span className="font-serif text-2xl text-ink">Batch {String(batch.id).padStart(2, '0')}</span>
                  <span className="text-sm text-ink-muted">{formatDate(batch.settlement_date)}</span>
                </div>
                <span className="text-sm text-brass">Open bridge →</span>
              </div>
              <div className="space-y-4 px-5 py-4">
                <StatusBanner batch={batch} />
                <MiniBridge batch={batch} />
                <p className="text-sm text-ink-muted">
                  Bank{' '}
                  <Amount value={batch.matched_bank_amount} className="text-ink" />
                  <span className="mx-2 text-rule">·</span>
                  Variance <Amount value={batch.variance} className="text-ink" />
                </p>
              </div>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  )
}

function Stat({ label, value, tone }) {
  const color = tone === 'forest' ? 'text-forest' : tone === 'rust' ? 'text-rust' : 'text-ink'
  return (
    <div>
      <dt className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-muted">{label}</dt>
      <dd className={`font-serif text-3xl leading-none ${color}`}>{value}</dd>
    </div>
  )
}
