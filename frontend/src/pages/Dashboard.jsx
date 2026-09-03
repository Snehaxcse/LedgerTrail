import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { getBatches, getDataSources, getStats } from '../api'
import MiniBridge from '../components/MiniBridge'
import StatusBanner from '../components/StatusBanner'
import Amount from '../components/Amount'
import { formatClassification, formatDate } from '../lib/format'

const SEVERITY_STYLES = {
  high: 'bg-rust-wash text-rust',
  medium: 'bg-amber-wash text-amber',
  low: 'bg-forest-wash text-forest',
  info: 'bg-rule/60 text-ink-muted',
}

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

  return (
    <div>
      <div className="mb-8">
        <p className="text-[11px] font-semibold uppercase tracking-[0.22em] text-brass">LedgerTrail</p>
        <h1 className="mt-1 font-serif text-4xl tracking-tight text-ink">What needs attention</h1>
        <p className="mt-2 max-w-2xl text-ink-muted">
          Turns unexplained settlement exceptions into evidence-backed investigations — without
          letting AI decide financial truth.
        </p>
      </div>

      {loading ? <p className="text-ink-muted">Loading…</p> : null}
      {error ? (
        <div className="rounded-sm border border-rust/40 bg-rust-wash px-4 py-3 text-rust">
          Cannot reach the LedgerTrail API. Start FastAPI on port 8000, then refresh.
          <p className="mt-1 font-mono text-xs opacity-80">{error}</p>
        </div>
      ) : null}

      {stats ? <AttentionMetrics stats={stats} /> : null}
      {stats ? <NeedsAttentionTable stats={stats} /> : null}

      <h2 className="mb-3 mt-10 text-[11px] font-semibold uppercase tracking-[0.2em] text-ink-muted">
        All batches
      </h2>
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

      {dataSources ? <DataSourcesPanel data={dataSources} /> : null}
    </div>
  )
}

function AttentionMetrics({ stats }) {
  const rate =
    stats.total_batches > 0
      ? Math.round((stats.batches_reconciled_automatically / stats.total_batches) * 100)
      : 0
  const hasAiCount = stats.ai_investigated_count != null
  return (
    <dl
      className={`grid grid-cols-2 gap-px overflow-hidden rounded-sm border border-ink/20 bg-rule ${
        hasAiCount ? 'sm:grid-cols-5' : 'sm:grid-cols-4'
      }`}
    >
      <Stat label="Amount at risk" value={<Amount value={stats.amount_at_risk} />} tone="rust" />
      <Stat label="Exceptions needing review" value={stats.exceptions_needing_review} tone="amber" />
      <Stat label="Auto-reconciled" value={`${stats.batches_reconciled_automatically}/${stats.total_batches} (${rate}%)`} tone="forest" />
      <Stat
        label="Oldest unresolved"
        value={stats.oldest_unresolved_days != null ? `${stats.oldest_unresolved_days}d` : '—'}
      />
      {hasAiCount ? <Stat label="AI-investigated" value={stats.ai_investigated_count} /> : null}
    </dl>
  )
}

function Stat({ label, value, tone }) {
  const color =
    tone === 'forest' ? 'text-forest' : tone === 'rust' ? 'text-rust' : tone === 'amber' ? 'text-amber' : 'text-ink'
  return (
    <div className="bg-paper-raised px-4 py-3">
      <dt className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-muted">{label}</dt>
      <dd className={`mt-0.5 font-serif text-2xl leading-tight sm:text-3xl ${color}`}>{value}</dd>
    </div>
  )
}

function NeedsAttentionTable({ stats }) {
  const rows = stats.needs_attention
  if (!rows.length) {
    return (
      <p className="mt-4 rounded-sm border border-forest/40 bg-forest-wash px-4 py-3 text-sm text-forest">
        Nothing needs your attention right now — every exception that requires a decision has one.
      </p>
    )
  }
  return (
    <section className="mt-4 overflow-x-auto rounded-sm border border-rule bg-paper-raised">
      <table className="w-full min-w-[560px] text-left text-sm">
        <thead>
          <tr className="border-b border-rule text-[11px] uppercase tracking-[0.14em] text-ink-muted">
            <th className="px-5 py-2 font-medium">Priority</th>
            <th className="px-5 py-2 font-medium">Exception</th>
            <th className="px-5 py-2 font-medium">Amount</th>
            <th className="px-5 py-2 font-medium">Age</th>
            <th className="px-5 py-2 font-medium">Action</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-rule">
          {rows.map((row) => (
            <tr key={row.exception_id}>
              <td className="px-5 py-3">
                <span
                  className={`inline-block rounded-sm px-2 py-0.5 text-[11px] font-semibold uppercase tracking-[0.14em] ${
                    SEVERITY_STYLES[row.severity] || SEVERITY_STYLES.info
                  }`}
                >
                  {row.severity || 'info'}
                </span>
              </td>
              <td className="px-5 py-3 text-ink">
                {formatClassification(row.classification)} <span className="text-ink-muted">(Batch {String(row.batch_id).padStart(2, '0')})</span>
              </td>
              <td className="px-5 py-3">
                <Amount value={row.unexplained_amount} className="text-ink" />
              </td>
              <td className="px-5 py-3 text-ink-muted">{row.age_days}d</td>
              <td className="px-5 py-3">
                <Link to={`/batches/${row.batch_id}`} className="text-brass underline">
                  Review →
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

function DataSourcesPanel({ data }) {
  const sources = data.sources ?? []
  if (sources.length === 0 && !data.note) return null

  return (
    <section className="mt-10 border border-rule bg-paper-raised">
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
