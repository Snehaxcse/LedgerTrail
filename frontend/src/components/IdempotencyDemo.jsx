import { useState } from 'react'
import { runHoldoutIdempotencyCheck } from '../api'

export default function IdempotencyDemo() {
  const [result, setResult] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  async function run() {
    setLoading(true)
    setError(null)
    try {
      const data = await runHoldoutIdempotencyCheck()
      setResult(data)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="mt-8 border border-rule bg-paper-raised px-6 py-6">
      <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-ink-muted">Idempotency</p>
      <h3 className="mt-1 font-serif text-xl tracking-tight text-ink">Replay settlement data</h3>
      <p className="mt-2 max-w-2xl text-sm text-ink-muted">
        Ingests the held-out dataset's 14 records into a fresh isolated database, then immediately
        ingests the exact same data again. Each record's source_event_id has a real database UNIQUE
        constraint — the second pass's duplicates are rejected by SQLite itself, not by an
        application-code "does this already exist" check.
      </p>

      {!result && !loading ? (
        <button
          type="button"
          onClick={run}
          className="mt-4 rounded-sm border border-rule px-4 py-2 text-sm text-ink hover:border-ink/40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-forest"
        >
          Replay settlement data
        </button>
      ) : null}

      {loading ? <p className="mt-4 text-sm text-ink-muted">Running two ingestion passes…</p> : null}

      {error ? (
        <p className="mt-4 text-sm text-rust">
          Could not run the idempotency check.
          <button type="button" onClick={run} className="ml-2 underline">
            Try again
          </button>
        </p>
      ) : null}

      {result ? (
        <div className="mt-5 space-y-2 text-sm text-ink">
          <p>
            First ingestion: <span className="font-semibold">{result.first_ingestion.accepted}</span> of{' '}
            {result.first_ingestion.records_seen} records accepted, {result.first_ingestion.duplicates}{' '}
            duplicates.
          </p>
          <p>
            Second ingestion: {result.second_ingestion.records_seen} records received,{' '}
            <span className="font-semibold">{result.second_ingestion.accepted}</span> newly accepted,{' '}
            <span className="font-semibold">{result.second_ingestion.duplicates}</span> rejected as
            duplicates.
          </p>
          <p
            className={`inline-block border px-3 py-1 font-semibold ${
              result.idempotent ? 'border-forest/40 bg-forest-wash text-forest' : 'border-rust/40 bg-rust-wash text-rust'
            }`}
          >
            Idempotency: {result.idempotent ? '✓ PASS' : '✕ FAIL'}
          </p>
          <button type="button" onClick={run} className="block text-xs text-ink-muted underline">
            Run again
          </button>
        </div>
      ) : null}
    </section>
  )
}
