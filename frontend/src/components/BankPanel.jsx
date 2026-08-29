import Amount from './Amount'
import { formatScore } from '../lib/format'

export default function BankPanel({ batch }) {
  const unmatched = batch.matched_bank_amount == null

  return (
    <section className="rounded-sm border border-rule bg-paper-raised">
      <header className="border-b border-rule px-5 py-3">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.2em] text-ink-muted">
          Bank credit
        </h2>
      </header>

      <div className="space-y-4 px-5 py-5">
        <div>
          <p className="text-sm uppercase tracking-[0.14em] text-ink-muted">Matched bank amount</p>
          <Amount value={batch.matched_bank_amount} className="mt-1 block text-xl font-semibold text-ink" />
        </div>

        <div>
          <p className="text-sm uppercase tracking-[0.14em] text-ink-muted">Variance</p>
          <Amount value={batch.variance} className="mt-1 block text-xl font-semibold text-ink" />
        </div>

        <dl className="grid grid-cols-2 gap-3 border-t border-rule pt-4 text-sm">
          <div>
            <dt className="text-ink-muted">Match type</dt>
            <dd className="mt-0.5 font-medium uppercase tracking-wide">
              {unmatched ? '—' : batch.match_type ?? '—'}
            </dd>
          </div>
          <div>
            <dt className="text-ink-muted">Confidence</dt>
            <dd className="mt-0.5 font-mono">{formatScore(batch.confidence_score)}</dd>
          </div>
        </dl>
      </div>
    </section>
  )
}
