import Amount from './Amount'

const DEDUCTIONS = [
  { key: 'total_refunds', label: 'Refunds' },
  { key: 'total_fees', label: 'Fees' },
  { key: 'total_tax', label: 'Tax' },
]

export default function BridgeStatement({ batch }) {
  return (
    <section className="rounded-sm border border-rule bg-paper-raised">
      <header className="border-b border-rule px-5 py-3">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.2em] text-ink-muted">
          Gross → Net bridge
        </h2>
      </header>

      <div className="px-5 py-5">
        <Row label="Gross" value={batch.total_gross} tone="start" />

        <div className="ml-3 border-l border-rule pl-5">
          {DEDUCTIONS.map((row) => (
            <Row key={row.key} label={row.label} value={batch[row.key]} tone="minus" />
          ))}
        </div>

        <div className="mt-2 border-t border-ink/20 pt-3">
          <Row label="Net" value={batch.total_net} tone="result" />
        </div>
      </div>
    </section>
  )
}

function Row({ label, value, tone }) {
  const prefix = tone === 'minus' ? '−' : tone === 'result' ? '=' : ''
  const size = tone === 'result' || tone === 'start' ? 'text-lg sm:text-xl' : 'text-base'

  return (
    <div className="flex items-baseline justify-between gap-4 py-2">
      <span className="text-sm uppercase tracking-[0.14em] text-ink-muted">
        {prefix ? <span className="mr-2 font-mono text-brass">{prefix}</span> : null}
        {label}
      </span>
      <Amount
        value={value}
        className={`${size} ${tone === 'result' ? 'font-semibold text-ink' : 'text-ink'}`}
      />
    </div>
  )
}
