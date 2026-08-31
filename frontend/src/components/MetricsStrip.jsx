export default function MetricsStrip({ stats }) {
  return (
    <dl className="mt-5 grid grid-cols-2 gap-px overflow-hidden rounded-sm border border-ink/20 bg-rule sm:grid-cols-5">
      <Stat label="Settlement entries" value={stats.total_settlement_entries} />
      <Stat label="Batches" value={stats.total_batches} />
      <Stat label="Reconciled automatically" value={stats.batches_reconciled_automatically} tone="forest" />
      <Stat label="Require human review" value={stats.batches_requiring_review} tone="amber" />
      <Stat label="Unsafe auto-resolutions" value={stats.unsafe_auto_resolutions} tone="forest" />
    </dl>
  )
}

function Stat({ label, value, tone }) {
  const color =
    tone === 'forest' ? 'text-forest' : tone === 'rust' ? 'text-rust' : tone === 'amber' ? 'text-amber' : 'text-ink'
  return (
    <div className="bg-paper-raised px-4 py-3">
      <dt className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-muted">{label}</dt>
      <dd className={`mt-0.5 font-serif text-3xl leading-none ${color}`}>
        {Number(value).toLocaleString('en-IN')}
      </dd>
    </div>
  )
}
