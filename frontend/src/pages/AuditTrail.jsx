import { useEffect, useState } from 'react'
import { getApprovers, getAuditTrail, getBatches, getExceptionIndex } from '../api'
import { auditEventDetails } from '../lib/audit'
import { formatDateTime } from '../lib/format'
import Amount from '../components/Amount'

const PAGE_SIZE = 50
const EMPTY_INDEX = { byId: {}, byClassification: {} }

const BADGE_TONE_STYLES = {
  forest: 'bg-forest-wash text-forest',
  rust: 'bg-rust-wash text-rust',
  amber: 'bg-amber-wash text-amber',
  brass: 'bg-rule/60 text-ink-muted',
}

export default function AuditTrail() {
  const [items, setItems] = useState([])
  const [index, setIndex] = useState(EMPTY_INDEX)
  const [batchesById, setBatchesById] = useState({})
  const [approversByName, setApproversByName] = useState({})
  const [total, setTotal] = useState(0)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    Promise.all([
      getAuditTrail({ limit: PAGE_SIZE, offset: 0 }),
      getExceptionIndex(),
      getBatches().catch(() => []),
      getApprovers().catch(() => []),
    ])
      .then(([data, exceptionIndex, batches, approvers]) => {
        if (cancelled) return
        setItems(data.items)
        setTotal(data.total)
        setIndex(exceptionIndex)
        setBatchesById(Object.fromEntries(batches.map((b) => [b.id, b])))
        setApproversByName(Object.fromEntries(approvers.map((p) => [p.name, p.role])))
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
            <AuditEventCard
              key={event.id}
              event={event}
              details={auditEventDetails(event, index, batchesById, approversByName)}
            />
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

function AuditEventCard({ event, details }) {
  return (
    <li className="px-5 py-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={`inline-block rounded-sm px-2 py-0.5 font-mono text-[11px] font-semibold tracking-[0.08em] ${BADGE_TONE_STYLES[details.badgeTone] || BADGE_TONE_STYLES.brass}`}
          >
            {details.badgeLabel}
          </span>
          <p className="text-sm text-ink">{details.subject}</p>
        </div>
        <div className="text-right">
          <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-brass">{event.actor}</p>
          <p className="text-xs text-ink-muted">{formatDateTime(event.timestamp)}</p>
        </div>
      </div>

      <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs text-ink-muted">
        {details.kind === 'reviewed' ? (
          <>
            <span>
              {details.actor}
              {details.role ? ` — ${details.role}` : ''}
            </span>
            {details.reason ? <span>Reason: {details.reason}</span> : null}
          </>
        ) : null}

        {details.kind === 'created' ? (
          <>
            {details.amount !== null ? (
              <span className="flex items-center gap-1">
                Unexplained <Amount value={details.amount} className="text-ink-muted" />
              </span>
            ) : null}
            {details.requiresApproval != null ? (
              <span>{details.requiresApproval ? 'Requires approval' : 'Informational only'}</span>
            ) : null}
          </>
        ) : null}

        {details.kind === 'matched' ? (
          <>
            {details.matchBasis ? <span>{details.matchBasis}</span> : null}
            {details.confidencePercent ? <span>{details.confidencePercent} confidence</span> : null}
            {details.bankAmount !== null ? (
              <span className="flex items-center gap-1">
                Bank <Amount value={details.bankAmount} className="text-ink-muted" />
              </span>
            ) : null}
            {details.variance !== null ? (
              <span className="flex items-center gap-1">
                Variance <Amount value={details.variance} className="text-ink-muted" />
              </span>
            ) : null}
          </>
        ) : null}
      </div>
    </li>
  )
}
