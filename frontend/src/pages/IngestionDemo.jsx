import { useState } from 'react'
import { Link } from 'react-router-dom'
import { replayRazorpaySettlement } from '../api'
import { invalidateBatches } from '../lib/dataEvents'

const STEPS = [
  { key: 'validated', label: 'Validated' },
  { key: 'normalized', label: 'Normalized' },
  { key: 'ingested', label: 'Ingested' },
  { key: 'reconciled', label: 'Reconciled' },
]

export default function IngestionDemo() {
  const [result, setResult] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  async function run() {
    setLoading(true)
    setError(null)
    try {
      const data = await replayRazorpaySettlement()
      setResult(data)
      // A genuinely new batch was created (not the duplicate-event path) --
      // tell any already-mounted batch-list/stats views to refetch, rather
      // than relying solely on their own next mount.
      if (!data.duplicate) invalidateBatches()
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div>
      <p className="text-[11px] font-semibold uppercase tracking-[0.22em] text-brass">
        Razorpay-Compatible Ingestion
      </p>
      <h1 className="mt-1 font-serif text-4xl tracking-tight text-ink">Ingestion demo</h1>
      <p className="mt-2 max-w-2xl text-sm leading-relaxed text-ink-muted">
        Not a live Razorpay integration — a Razorpay-compatible ingestion adapter fed a fixed
        synthetic settlement event, wired to the real backend and the real database. Every
        settlement event carries a unique identifier, enforced by the database itself — replaying
        the same event never creates a second batch.
      </p>

      <section className="mt-6 max-w-2xl rounded-sm border border-rule bg-paper-raised px-6 py-6">
        <button
          type="button"
          onClick={run}
          disabled={loading}
          className="rounded-sm border border-rule px-4 py-2 text-sm text-ink hover:border-ink/40 disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-forest"
        >
          {loading ? 'Replaying…' : 'Replay Razorpay settlement'}
        </button>

        {error ? (
          <p className="mt-4 text-sm text-rust">
            Could not run ingestion.
            <button type="button" onClick={run} className="ml-2 underline">
              Try again
            </button>
          </p>
        ) : null}

        {result ? (
          <div className="mt-5 space-y-3 text-sm text-ink">
            {result.duplicate ? (
              <p className="border-2 border-rust bg-rust-wash px-4 py-3 font-semibold text-rust">
                DUPLICATE EVENT — Already processed. No duplicate financial state created.
              </p>
            ) : (
              <div className="flex flex-wrap gap-2">
                {STEPS.map((step) => (
                  <span
                    key={step.key}
                    className="inline-block rounded-sm border border-forest/40 bg-forest-wash px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.14em] text-forest"
                  >
                    ✓ {step.label}
                  </span>
                ))}
              </div>
            )}

            {result.duplicate ? null : <p className="text-ink-muted">{result.message}</p>}

            <dl className="grid grid-cols-2 gap-x-4 gap-y-1 border-t border-rule pt-3 text-xs text-ink-muted">
              <dt>Source event ID</dt>
              <dd className="text-right font-mono text-ink">{result.source_event_id}</dd>
              <dt>Batch</dt>
              <dd className="text-right text-ink">
                <Link to={`/batches/${result.batch_id}`} className="text-brass underline">
                  Batch {String(result.batch_id).padStart(2, '0')}
                </Link>
              </dd>
              {result.exceptions_created.length ? (
                <>
                  <dt>Exceptions raised</dt>
                  <dd className="text-right text-ink">{result.exceptions_created.length}</dd>
                </>
              ) : null}
            </dl>

            <button type="button" onClick={run} className="block text-xs text-ink-muted underline">
              Replay again
            </button>
          </div>
        ) : null}
      </section>
    </div>
  )
}
