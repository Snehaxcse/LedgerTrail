import Amount from './Amount'
import { formatMatchLine } from '../lib/format'

export default function BankPanel({ batch }) {
  const unmatched = batch.matched_bank_amount == null
  const matchLine = formatMatchLine(batch.match_type, batch.confidence_score)

  return (
    <section className="rounded-sm border border-rule bg-paper-raised">
      <header className="border-b border-rule px-5 py-3">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.2em] text-ink-muted">
          Bank credit
        </h2>
      </header>

      <div className="space-y-4 px-5 py-5">
        {unmatched ? (
          <div>
            <p className="text-sm uppercase tracking-[0.14em] text-ink-muted">Bank match</p>
            <p className="mt-1 text-lg font-semibold text-rust">Unmatched</p>
            <p className="mt-1 text-sm text-ink-muted">No bank credit is linked to this batch.</p>
          </div>
        ) : (
          <div>
            <p className="text-sm uppercase tracking-[0.14em] text-ink-muted">Matched bank amount</p>
            <Amount value={batch.matched_bank_amount} className="mt-1 block text-xl font-semibold text-ink" />
            {matchLine ? <p className="mt-1 text-sm text-ink-muted">{matchLine}</p> : null}
          </div>
        )}

        <div>
          <p className="text-sm uppercase tracking-[0.14em] text-ink-muted">Variance</p>
          <Amount value={batch.variance} className="mt-1 block text-xl font-semibold text-ink" />
        </div>
      </div>
    </section>
  )
}
