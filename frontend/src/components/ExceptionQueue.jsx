import { useMemo, useRef, useState } from 'react'
import Amount from './Amount'
import AiExplanation from './AiExplanation'
import { formatClassification } from '../lib/format'

const STATUS_STYLES = {
  open: 'bg-amber-wash text-amber',
  approved: 'bg-forest-wash text-forest',
  rejected: 'bg-rust-wash text-rust',
}

const SEVERITY_RANK = { high: 0, medium: 1, low: 2, info: 3 }

const SEVERITY_STYLES = {
  high: 'bg-rust-wash text-rust',
  medium: 'bg-amber-wash text-amber',
  low: 'bg-forest-wash text-forest',
  info: 'bg-rule/60 text-ink-muted',
}

const SEVERITY_FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'high', label: 'High' },
  { id: 'medium', label: 'Medium' },
  { id: 'low', label: 'Low' },
  { id: 'info', label: 'Info' },
]

export default function ExceptionQueue({
  batchId,
  exceptions,
  approver,
  onApproverChange,
  reasons,
  onReasonChange,
  onReview,
  pendingId,
  error,
}) {
  const reasonRefs = useRef({})
  const [missingReasonId, setMissingReasonId] = useState(null)
  const [severityFilter, setSeverityFilter] = useState('all')
  const blockingOpen = exceptions.filter(
    (row) => row.status === 'open' && row.requires_approval,
  )
  const visible = useMemo(() => {
    const ranked = [...exceptions].sort((a, b) => {
      const left = SEVERITY_RANK[a.severity] ?? 99
      const right = SEVERITY_RANK[b.severity] ?? 99
      if (left !== right) return left - right
      return a.id - b.id
    })
    if (severityFilter === 'all') return ranked
    return ranked.filter((row) => row.severity === severityFilter)
  }, [exceptions, severityFilter])

  function handleReject(row) {
    const text = (reasons[row.id] ?? '').trim()
    if (!approver.trim() || pendingId != null) return
    if (!text) {
      setMissingReasonId(row.id)
      reasonRefs.current[row.id]?.focus()
      return
    }
    setMissingReasonId(null)
    onReview(row.id, 'rejected', text)
  }

  return (
    <section className="rounded-sm border border-rule bg-paper-raised">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-rule px-5 py-3">
        <div>
          <h2 className="text-[11px] font-semibold uppercase tracking-[0.2em] text-ink-muted">
            Exception queue
          </h2>
          <p className="mt-1 text-sm text-ink-muted">
            {blockingOpen.length === 0
              ? 'Nothing in this queue is blocking close.'
              : blockingOpen.length === 1
                ? '1 open exception still requires a decision before this batch can close.'
                : `${blockingOpen.length} open exceptions still require a decision before this batch can close.`}
          </p>
        </div>
        <div className="flex flex-wrap items-end gap-4">
          <div className="min-w-0" role="group" aria-label="Severity">
            <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-muted">
              Severity
            </p>
            <div className="mt-1 flex flex-wrap gap-1">
              {SEVERITY_FILTERS.map((option) => {
                const active = severityFilter === option.id
                return (
                  <button
                    key={option.id}
                    type="button"
                    aria-pressed={active}
                    onClick={() => setSeverityFilter(option.id)}
                    className={`rounded-sm px-2.5 py-1 text-xs font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-forest ${
                      active
                        ? 'bg-ink text-paper-raised'
                        : 'border border-rule bg-paper text-ink-muted hover:text-ink'
                    }`}
                  >
                    {option.label}
                  </button>
                )
              })}
            </div>
          </div>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-muted">
              Approver
            </span>
            <input
              type="text"
              value={approver}
              onChange={(event) => onApproverChange(event.target.value)}
              placeholder="Your name"
              autoComplete="name"
              className="w-56 rounded-sm border border-rule bg-paper px-3 py-1.5 text-ink outline-none focus:border-forest"
            />
          </label>
        </div>
      </header>

      {error ? (
        <p className="border-b border-rust/30 bg-rust-wash px-5 py-2 text-sm text-rust">{error}</p>
      ) : null}

      {exceptions.length === 0 ? (
        <p className="px-5 py-6 text-sm text-ink-muted">No exceptions on this batch.</p>
      ) : visible.length === 0 ? (
        <p className="px-5 py-6 text-sm text-ink-muted">No exceptions at this severity.</p>
      ) : (
        <ul className="divide-y divide-rule">
          {visible.map((row) => {
            const draftReason = reasons[row.id] ?? ''
            const reasonMissing = missingReasonId === row.id
            return (
              <li key={row.id} className="px-5 py-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 max-w-2xl">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="font-medium text-ink">{formatClassification(row.classification)}</p>
                      <SeverityBadge severity={row.severity} />
                    </div>
                    <ReviewSummary exception={row} />
                    <p className="mt-2 text-sm leading-snug text-ink-muted">{row.suggested_action}</p>
                  </div>
                  <div className="text-right">
                    <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-muted">
                      Unexplained
                    </p>
                    <Amount value={row.unexplained_amount} className="mt-0.5 block text-base text-ink" />
                  </div>
                </div>

                <div className="mt-3">
                  {row.status !== 'open' ? null : !row.requires_approval ? (
                    <p className="text-sm text-ink-muted">Informational — no approval required.</p>
                  ) : (
                    <div className="space-y-2">
                      <label className="block max-w-xl text-sm">
                        <span className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-muted">
                          Reason (required to reject)
                        </span>
                        <textarea
                          ref={(node) => {
                            reasonRefs.current[row.id] = node
                          }}
                          value={draftReason}
                          onChange={(event) => {
                            if (missingReasonId === row.id) setMissingReasonId(null)
                            onReasonChange(row.id, event.target.value)
                          }}
                          rows={2}
                          placeholder="Why this exception is being rejected"
                          className={`mt-1 w-full resize-y rounded-sm border bg-paper px-3 py-1.5 text-ink outline-none focus:border-forest ${
                            reasonMissing ? 'border-rust' : 'border-rule'
                          }`}
                        />
                      </label>
                      <div className="flex flex-wrap gap-2">
                        <button
                          type="button"
                          disabled={!approver.trim() || pendingId != null}
                          onClick={() => onReview(row.id, 'approved')}
                          className="rounded-sm bg-forest px-3 py-1.5 text-sm font-medium text-paper-raised disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-forest"
                        >
                          {pendingId === row.id ? 'Saving…' : 'Approve'}
                        </button>
                        <button
                          type="button"
                          disabled={!approver.trim() || pendingId != null}
                          onClick={() => handleReject(row)}
                          className="rounded-sm border border-rust px-3 py-1.5 text-sm font-medium text-rust disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rust"
                        >
                          Reject
                        </button>
                        {!approver.trim() ? (
                          <span className="self-center text-xs text-ink-muted">
                            Enter an approver name to decide.
                          </span>
                        ) : reasonMissing ? (
                          <span className="self-center text-xs text-rust">
                            Enter a reason before rejecting.
                          </span>
                        ) : null}
                      </div>
                    </div>
                  )}
                </div>

                <AiExplanation batchId={batchId} exceptionId={row.id} />
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

function SeverityBadge({ severity }) {
  if (!severity || !SEVERITY_STYLES[severity]) return null
  return (
    <span
      className={`inline-block rounded-sm px-2 py-0.5 text-[11px] font-semibold uppercase tracking-[0.14em] ${SEVERITY_STYLES[severity]}`}
    >
      {severity}
    </span>
  )
}

function ReviewSummary({ exception }) {
  if (exception.status === 'open') {
    return (
      <p className={`mt-1 inline-block rounded-sm px-2 py-0.5 text-[11px] font-semibold uppercase tracking-[0.14em] ${STATUS_STYLES.open}`}>
        open
      </p>
    )
  }

  const who = exception.approver || 'unknown'
  if (exception.status === 'rejected') {
    return (
      <p className="mt-1 text-sm text-rust">
        Rejected by {who}
        {exception.reason ? `: ${exception.reason}` : ''}
      </p>
    )
  }

  if (exception.status === 'approved') {
    return (
      <p className="mt-1 text-sm text-forest">
        Approved by {who}
        {exception.reason ? `: ${exception.reason}` : ''}
      </p>
    )
  }

  return (
    <p className={`mt-1 inline-block rounded-sm px-2 py-0.5 text-[11px] font-semibold uppercase tracking-[0.14em] ${STATUS_STYLES.open}`}>
      {exception.status}
    </p>
  )
}
