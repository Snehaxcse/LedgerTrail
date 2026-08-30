import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { getBankTransactions } from '../api'
import Amount from '../components/Amount'
import NarrationVerify from '../components/NarrationVerify'
import { formatDate } from '../lib/format'

export default function BankStatement() {
  const [rows, setRows] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    getBankTransactions()
      .then((data) => {
        if (!cancelled) setRows(data)
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
      <p className="text-[11px] font-semibold uppercase tracking-[0.22em] text-brass">Bank</p>
      <h1 className="mt-1 font-serif text-4xl tracking-tight text-ink">Bank statement</h1>
      <p className="mt-2 max-w-2xl text-ink-muted">
        Every line on the statement for this period, including activity that is not a
        Razorpay settlement. Matching links settlement credits to batches and leaves
        the rest unmatched. Narration verification is on demand and does not change
        matching.
      </p>

      {loading ? <p className="mt-8 text-ink-muted">Loading statement…</p> : null}
      {error ? (
        <div className="mt-6 rounded-sm border border-rust/40 bg-rust-wash px-4 py-3 text-rust">
          Could not load the bank statement.
          <p className="mt-1 font-mono text-xs opacity-80">{error}</p>
        </div>
      ) : null}

      {!loading && !error && rows.length === 0 ? (
        <p className="mt-8 text-ink-muted">No bank transactions on file.</p>
      ) : null}

      {rows.length ? (
        <section className="mt-8 overflow-x-auto rounded-sm border border-rule bg-paper-raised">
          <table className="w-full min-w-[720px] text-left text-sm">
            <thead>
              <tr className="border-b border-rule text-[11px] uppercase tracking-[0.14em] text-ink-muted">
                <th className="px-5 py-2 font-medium">Date</th>
                <th className="px-5 py-2 font-medium">Amount</th>
                <th className="px-5 py-2 font-medium">Reference</th>
                <th className="px-5 py-2 font-medium">Narration</th>
                <th className="px-5 py-2 font-medium">Match</th>
                <th className="px-5 py-2 font-medium">Narration check</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className="border-b border-rule/70 align-top last:border-0">
                  <td className="whitespace-nowrap px-5 py-3">{formatDate(row.date)}</td>
                  <td className="whitespace-nowrap px-5 py-3">
                    <Amount value={row.amount} />
                  </td>
                  <td className="px-5 py-3 font-mono text-xs">{row.reference}</td>
                  <td className="max-w-sm px-5 py-3 leading-relaxed text-ink">
                    {row.description || '—'}
                  </td>
                  <td className="whitespace-nowrap px-5 py-3">
                    {row.matched_batch_id != null ? (
                      <Link
                        to={`/batches/${row.matched_batch_id}`}
                        className="text-forest no-underline hover:underline"
                      >
                        Batch {String(row.matched_batch_id).padStart(2, '0')}
                      </Link>
                    ) : (
                      <span className="text-ink-muted">Unmatched</span>
                    )}
                  </td>
                  <td className="px-5 py-3">
                    <NarrationVerify bankTransactionId={row.id} compact />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}
    </div>
  )
}
