import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { getBatch, getBatchExceptions, reviewException } from '../api'
import BankPanel from '../components/BankPanel'
import BridgeStatement from '../components/BridgeStatement'
import Amount from '../components/Amount'
import ExceptionQueue from '../components/ExceptionQueue'
import SimulatedNotice from '../components/SimulatedNotice'
import StatusBanner from '../components/StatusBanner'
import { formatClassification, formatDate } from '../lib/format'

const APPROVER_KEY = 'ledgertrail.approver'

export default function BatchBridge() {
  const { id } = useParams()
  const [batch, setBatch] = useState(null)
  const [exceptions, setExceptions] = useState([])
  const [error, setError] = useState(null)
  const [reviewError, setReviewError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [pendingId, setPendingId] = useState(null)
  const [approver, setApprover] = useState(() => sessionStorage.getItem(APPROVER_KEY) || '')
  const [reasons, setReasons] = useState({})
  const [notice, setNotice] = useState(null)
  const dismissNotice = useCallback(() => setNotice(null), [])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    setReviewError(null)
    setNotice(null)
    Promise.all([getBatch(id), getBatchExceptions(id)])
      .then(([batchData, exceptionData]) => {
        if (!cancelled) {
          setBatch(batchData)
          setExceptions(exceptionData)
        }
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
  }, [id])

  function handleApproverChange(value) {
    setApprover(value)
    sessionStorage.setItem(APPROVER_KEY, value)
  }

  function handleReasonChange(exceptionId, value) {
    setReasons((current) => ({ ...current, [exceptionId]: value }))
  }

  async function handleReview(exceptionId, decision, reason) {
    const name = approver.trim()
    if (!name || pendingId != null) return
    if (decision === 'rejected' && !String(reason ?? '').trim()) return

    setPendingId(exceptionId)
    setReviewError(null)
    const reviewed = exceptions.find((row) => row.id === exceptionId)
    try {
      await reviewException(exceptionId, {
        approver: name,
        decision,
        reason: decision === 'rejected' ? String(reason).trim() : undefined,
      })
      const [nextBatch, nextExceptions] = await Promise.all([
        getBatch(id),
        getBatchExceptions(id),
      ])
      setBatch(nextBatch)
      setExceptions(nextExceptions)
      setNotice({
        token: `${exceptionId}-${decision}-${Date.now()}`,
        kind: decision,
        classification: formatClassification(reviewed?.classification) || 'this exception',
      })
    } catch (err) {
      setReviewError(err.message)
    } finally {
      setPendingId(null)
    }
  }

  return (
    <div>
      <Link
        to="/"
        className="text-sm font-medium text-brass no-underline hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-forest"
      >
        ← All batches
      </Link>

      {loading ? <p className="mt-8 text-ink-muted">Loading bridge…</p> : null}
      {error ? (
        <div className="mt-8 rounded-sm border border-rust/40 bg-rust-wash px-4 py-3 text-rust">
          Could not load this batch.
          <p className="mt-1 font-mono text-xs opacity-80">{error}</p>
        </div>
      ) : null}

      {batch ? (
        <article className="mt-6 space-y-6">
          <header>
            <p className="text-[11px] font-semibold uppercase tracking-[0.22em] text-brass">
              {formatDate(batch.settlement_date)}
            </p>
            <h1 className="mt-1 font-serif text-4xl tracking-tight text-ink">
              Batch {String(batch.id).padStart(2, '0')}
            </h1>
          </header>

          <StatusBanner batch={batch} size="full" />

          <ExceptionQueue
            batchId={batch.id}
            exceptions={exceptions}
            approver={approver}
            onApproverChange={handleApproverChange}
            reasons={reasons}
            onReasonChange={handleReasonChange}
            onReview={handleReview}
            pendingId={pendingId}
            error={reviewError}
          />

          <div className="grid gap-4 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
            <BridgeStatement batch={batch} />
            <BankPanel batch={batch} />
          </div>

          {batch.entries?.length ? (
            <section className="rounded-sm border border-rule bg-paper-raised">
              <header className="border-b border-rule px-5 py-3">
                <h2 className="text-[11px] font-semibold uppercase tracking-[0.2em] text-ink-muted">
                  Settlement lines
                </h2>
              </header>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[640px] text-left text-sm">
                  <thead>
                    <tr className="border-b border-rule text-[11px] uppercase tracking-[0.14em] text-ink-muted">
                      <th className="px-5 py-2 font-medium">Order</th>
                      <th className="px-5 py-2 font-medium">Gross</th>
                      <th className="px-5 py-2 font-medium">Refund</th>
                      <th className="px-5 py-2 font-medium">Fee</th>
                      <th className="px-5 py-2 font-medium">Tax</th>
                      <th className="px-5 py-2 font-medium">Net</th>
                    </tr>
                  </thead>
                  <tbody>
                    {batch.entries.map((entry) => (
                      <tr key={entry.id} className="border-b border-rule/70 last:border-0">
                        <td className="px-5 py-2 font-mono text-xs">{entry.order_ref}</td>
                        <td className="px-5 py-2">
                          <Amount value={entry.gross_amount} />
                        </td>
                        <td className="px-5 py-2">
                          <Amount value={entry.refund} />
                        </td>
                        <td className="px-5 py-2">
                          <Amount value={entry.fee} />
                        </td>
                        <td className="px-5 py-2">
                          <Amount value={entry.tax} />
                        </td>
                        <td className="px-5 py-2">
                          <Amount value={entry.net_amount} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          ) : null}
        </article>
      ) : null}

      <SimulatedNotice notice={notice} onDismiss={dismissNotice} />
    </div>
  )
}
