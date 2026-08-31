import { useEffect, useState } from 'react'
import { getHeroCase, investigateHeroCase } from '../api'
import Amount from '../components/Amount'
import BankPanel from '../components/BankPanel'
import BridgeStatement from '../components/BridgeStatement'
import InvestigationTrace from '../components/InvestigationTrace'
import StatusBanner from '../components/StatusBanner'
import { formatClassification, formatDate } from '../lib/format'

const SEVERITY_STYLES = {
  high: 'bg-rust-wash text-rust',
  medium: 'bg-amber-wash text-amber',
  low: 'bg-forest-wash text-forest',
  info: 'bg-rule/60 text-ink-muted',
}

export default function AgentDemo() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    getHeroCase()
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

  return (
    <div>
      <p className="text-[11px] font-semibold uppercase tracking-[0.22em] text-brass">
        AI Reconciliation Investigation Agent
      </p>
      <h1 className="mt-1 font-serif text-4xl tracking-tight text-ink">Investigation demo</h1>
      <p className="mt-2 max-w-2xl text-sm leading-relaxed text-ink-muted">
        A constructed batch in an isolated demo database — not part of the primary dataset shown
        elsewhere in this app. It exists to show the investigation agent working through a
        genuinely multi-order case: several tool calls, a hypothesis, and a deterministic check
        of that hypothesis against the underlying records before anything is shown as fact.
      </p>

      {loading ? <p className="mt-8 text-ink-muted">Loading demo case…</p> : null}
      {error ? (
        <div className="mt-8 rounded-sm border border-rust/40 bg-rust-wash px-4 py-3 text-rust">
          Could not load the demo case.
          <p className="mt-1 font-mono text-xs opacity-80">{error}</p>
        </div>
      ) : null}

      {data ? (
        <article className="mt-6 space-y-6">
          <header>
            <p className="text-[11px] font-semibold uppercase tracking-[0.22em] text-brass">
              {formatDate(data.batch.settlement_date)}
            </p>
            <h2 className="mt-1 font-serif text-3xl tracking-tight text-ink">
              Batch {String(data.batch.id).padStart(2, '0')} — demo data
            </h2>
          </header>

          <StatusBanner batch={data.batch} size="full" />

          <section className="rounded-sm border border-rule bg-paper-raised">
            <header className="border-b border-rule px-5 py-3">
              <h2 className="text-[11px] font-semibold uppercase tracking-[0.2em] text-ink-muted">
                Exceptions on this batch
              </h2>
            </header>
            <ul className="divide-y divide-rule">
              {data.exceptions.map((row) => {
                const isHeroException = row.id === data.investigate_exception_id
                return (
                  <li key={row.id} className="px-5 py-4">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0 max-w-2xl">
                        <div className="flex flex-wrap items-center gap-2">
                          <p className="font-medium text-ink">{formatClassification(row.classification)}</p>
                          {row.severity && SEVERITY_STYLES[row.severity] ? (
                            <span
                              className={`inline-block rounded-sm px-2 py-0.5 text-[11px] font-semibold uppercase tracking-[0.14em] ${SEVERITY_STYLES[row.severity]}`}
                            >
                              {row.severity}
                            </span>
                          ) : null}
                          {isHeroException ? (
                            <span className="inline-block rounded-sm bg-ink px-2 py-0.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-paper-raised">
                              investigated below
                            </span>
                          ) : null}
                        </div>
                        <p className="mt-2 text-sm leading-snug text-ink-muted">{row.suggested_action}</p>
                      </div>
                      <div className="text-right">
                        <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-muted">
                          Unexplained
                        </p>
                        <Amount value={row.unexplained_amount} className="mt-0.5 block text-base text-ink" />
                      </div>
                    </div>

                    {isHeroException ? (
                      <InvestigationTrace
                        fetcher={investigateHeroCase}
                        buttonLabel="Run the investigation agent"
                      />
                    ) : (
                      <p className="mt-3 text-xs text-ink-muted">
                        Not wired to the demo investigation endpoint — shown for batch context only.
                      </p>
                    )}
                  </li>
                )
              })}
            </ul>
          </section>

          <div className="grid gap-4 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
            <BridgeStatement batch={data.batch} />
            <BankPanel batch={data.batch} />
          </div>
        </article>
      ) : null}
    </div>
  )
}
