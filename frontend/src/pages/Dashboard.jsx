import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { getBatches, getDataSources, getStats } from '../api'
import MiniBridge from '../components/MiniBridge'
import StatusBanner from '../components/StatusBanner'
import Amount from '../components/Amount'
import { formatDate } from '../lib/format'

export default function Dashboard() {
  const [batches, setBatches] = useState([])
  const [stats, setStats] = useState(null)
  const [dataSources, setDataSources] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    Promise.all([
      getBatches(),
      getStats().catch(() => null),
      getDataSources().catch(() => null),
    ])
      .then(([batchData, statsData, sourcesData]) => {
        if (cancelled) return
        setBatches(batchData)
        setStats(statsData)
        setDataSources(sourcesData)
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
            Status reflects whether every open exception is resolved — not just whether the bank amount
            matches.
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

      {dataSources ? <DataSourcesPanel data={dataSources} /> : null}
      {stats ? <StatsCard stats={stats} /> : null}

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

function DataSourcesPanel({ data }) {
  const sources = data.sources ?? []
  if (sources.length === 0 && !data.note) return null

  return (
    <section className="mb-6 border border-rule bg-paper-raised">
      <header className="border-b border-rule px-5 py-3">
        <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-ink-muted">
          Data sources
        </p>
      </header>
      <ul className="grid gap-px bg-rule sm:grid-cols-3">
        {sources.map((source) => (
          <li key={source.name} className="bg-paper-raised px-5 py-4">
            <p className="font-serif text-xl tracking-tight text-ink">{source.name}</p>
            <p className="mt-1 text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-muted">
              {source.format}
            </p>
            <p className="mt-3 font-serif text-2xl leading-none text-ink">
              {Number(source.record_count).toLocaleString('en-IN')}
            </p>
            <p className="mt-0.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-muted">
              Records
            </p>
            <p className="mt-3 text-sm leading-relaxed text-ink-muted">{source.description}</p>
          </li>
        ))}
      </ul>
      {data.note ? (
        <p className="border-t border-dashed border-brass/70 bg-amber-wash px-5 py-3 text-sm leading-relaxed text-ink">
          {data.note}
        </p>
      ) : null}
    </section>
  )
}

function StatsCard({ stats }) {
  const saved = stats.time_saved
  const minutes = saved?.estimated_minutes_saved

  return (
    <section className="mb-6 border border-ink/20 bg-paper-raised px-5 py-5">
      <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-ink-muted">
        This pass
      </p>
      <p className="mt-2 font-serif text-2xl tracking-tight text-ink sm:text-3xl">
        {stats.batches_reconciled_automatically} of {stats.total_batches} batches
        reconciled automatically, {stats.batches_requiring_review} required human
        review
      </p>
      {minutes != null ? (
        <div className="mt-4 border-t border-rule pt-3">
          <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-brass">
            Estimated time saved
          </p>
          <p className="mt-1 font-serif text-2xl text-ink">{minutes} minutes</p>
          <p className="mt-1 max-w-2xl text-xs leading-relaxed text-ink-muted">
            {saved?.assumption || 'An estimate only — not a measured or verified figure.'}
          </p>
        </div>
      ) : null}
    </section>
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
