import { useEffect, useState } from 'react'
import { getAuditTrail, getExceptionIndex } from '../api'
import { describeAuditEvent } from '../lib/audit'
import { formatDateTime } from '../lib/format'

const PAGE_SIZE = 50
const EMPTY_INDEX = { byId: {}, byClassification: {} }

export default function AuditTrail() {
  const [items, setItems] = useState([])
  const [index, setIndex] = useState(EMPTY_INDEX)
  const [total, setTotal] = useState(0)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    Promise.all([getAuditTrail({ limit: PAGE_SIZE, offset: 0 }), getExceptionIndex()])
      .then(([data, exceptionIndex]) => {
        if (cancelled) return
        setItems(data.items)
        setTotal(data.total)
        setIndex(exceptionIndex)
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

  async function loadMore() {
    setLoadingMore(true)
    setError(null)
    try {
      const data = await getAuditTrail({ limit: PAGE_SIZE, offset: items.length })
      setItems((current) => [...current, ...data.items])
      setTotal(data.total)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoadingMore(false)
    }
  }

  return (
    <div>
      <p className="text-[11px] font-semibold uppercase tracking-[0.22em] text-brass">History</p>
      <h1 className="mt-1 font-serif text-4xl tracking-tight text-ink">Audit trail</h1>
      <p className="mt-2 max-w-xl text-ink-muted">
        Append-only record of matching, exception creation, and human review. Most recent first.
      </p>

      {loading ? <p className="mt-8 text-ink-muted">Loading audit trail…</p> : null}
      {error ? (
        <div className="mt-6 rounded-sm border border-rust/40 bg-rust-wash px-4 py-3 text-rust">
          Could not load the audit trail.
          <p className="mt-1 font-mono text-xs opacity-80">{error}</p>
        </div>
      ) : null}

      {!loading && items.length === 0 && !error ? (
        <p className="mt-8 text-ink-muted">No audit events yet.</p>
      ) : null}

      {items.length ? (
        <ol className="mt-8 divide-y divide-rule rounded-sm border border-rule bg-paper-raised">
          {items.map((event) => (
            <li key={event.id} className="px-5 py-4">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <p className="text-sm text-ink-muted">{formatDateTime(event.timestamp)}</p>
                <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-brass">
                  {event.actor}
                </p>
              </div>
              <p className="mt-1 text-base text-ink">{describeAuditEvent(event, index)}</p>
            </li>
          ))}
        </ol>
      ) : null}

      {items.length < total ? (
        <button
          type="button"
          onClick={loadMore}
          disabled={loadingMore}
          className="mt-4 rounded-sm border border-rule px-4 py-2 text-sm text-ink disabled:opacity-40"
        >
          {loadingMore ? 'Loading…' : `Load more (${items.length} of ${total})`}
        </button>
      ) : null}
    </div>
  )
}
