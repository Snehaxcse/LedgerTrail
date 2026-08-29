import { useState } from 'react'
import { getExceptionEvidence } from '../api'
import Amount from './Amount'
import { formatDate, formatInr, formatPercentPoints, formatRatePercent } from '../lib/format'

const ANOMALY_CLASSIFICATIONS = new Set(['SYSTEMIC_FEE_DRIFT', 'SYSTEMIC_REFUND_DRIFT'])

function metricLabel(metric) {
  if (metric === 'fee_rate') return 'fee rate'
  if (metric === 'refund_rate') return 'refund rate'
  return String(metric || 'rate').replaceAll('_', ' ')
}

function formatBatchIds(ids) {
  if (!ids?.length) return 'earlier batches'
  return ids.map((id) => String(id).padStart(2, '0')).join(', ')
}

function amountsDiffer(left, right) {
  return formatInr(left) !== formatInr(right)
}

function ordersByRef(orders) {
  const map = {}
  for (const order of orders) map[order.order_ref] = order
  return map
}

export default function EvidencePanel({ batchId, exceptionId, classification }) {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const payload = await getExceptionEvidence(batchId, exceptionId)
      setData(payload)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  function toggle() {
    const next = !open
    setOpen(next)
    if (next && data == null && !loading) load()
  }

  return (
    <div className="mt-4 max-w-3xl border-t border-rule pt-3">
      <button
        type="button"
        aria-expanded={open}
        onClick={toggle}
        className="rounded-sm border border-rule px-3 py-1.5 text-sm text-ink hover:border-ink/40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-forest"
      >
        {open ? 'Hide evidence' : 'View evidence'}
      </button>

      {open ? (
        <div className="mt-3">
          <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-muted">
            Source data
          </p>

          {loading ? <p className="mt-2 text-sm text-ink-muted">Loading source data…</p> : null}

          {error ? (
            <p className="mt-2 text-sm text-rust">
              Could not load the source data.
              <button type="button" onClick={load} className="ml-2 underline">
                Try again
              </button>
            </p>
          ) : null}

          {data ? <EvidenceBody classification={classification} data={data} /> : null}
        </div>
      ) : null}
    </div>
  )
}

function EvidenceBody({ classification, data }) {
  if (ANOMALY_CLASSIFICATIONS.has(classification) || isAnomalyPayload(data)) {
    return <AnomalyComparison data={data} />
  }

  const entries = data.settlement_entries ?? []
  const orders = data.order_records ?? []
  const banks = data.bank_transactions ?? []
  const empty = entries.length === 0 && orders.length === 0 && banks.length === 0

  if (empty) {
    return <p className="mt-2 text-sm text-ink-muted">No linked source rows for this exception.</p>
  }

  return (
    <div className="mt-3 space-y-4">
      <MismatchCallout classification={classification} entries={entries} orders={orders} />

      {entries.length ? (
        <section>
          <h3 className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-muted">
            Settlement entry
          </h3>
          <div className="mt-2 overflow-x-auto">
            <table className="w-full min-w-[36rem] text-left text-sm">
              <thead>
                <tr className="border-b border-rule text-[11px] uppercase tracking-[0.14em] text-ink-muted">
                  <th className="py-1.5 pr-3 font-medium">Order</th>
                  <th className="py-1.5 pr-3 font-medium">Gross</th>
                  <th className="py-1.5 pr-3 font-medium">Fee</th>
                  <th className="py-1.5 pr-3 font-medium">Tax</th>
                  <th className="py-1.5 pr-3 font-medium">Refund</th>
                  <th className="py-1.5 font-medium">Net</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((entry) => {
                  const order = ordersByRef(orders)[entry.order_ref]
                  const refundOff =
                    classification === 'MISSING_REFUND_RECORD' &&
                    amountsDiffer(entry.refund, order?.refund_amount ?? null)
                  const feeOff =
                    classification === 'FEE_TIER_MISMATCH' &&
                    amountsDiffer(entry.fee, order?.fee_amount)
                  const dupeOff = isDuplicateRef(entries, entry.order_ref)
                  return (
                    <tr key={entry.id} className="border-b border-rule/70 last:border-0">
                      <td className={`py-1.5 pr-3 font-mono text-xs ${dupeOff ? 'bg-rust-wash text-rust' : ''}`}>
                        {entry.order_ref}
                      </td>
                      <td className="py-1.5 pr-3">
                        <Amount value={entry.gross_amount} />
                      </td>
                      <td className={`py-1.5 pr-3 ${feeOff ? 'bg-rust-wash' : ''}`}>
                        <Amount value={entry.fee} className={feeOff ? 'text-rust' : ''} />
                      </td>
                      <td className="py-1.5 pr-3">
                        <Amount value={entry.tax} />
                      </td>
                      <td className={`py-1.5 pr-3 ${refundOff ? 'bg-rust-wash' : ''}`}>
                        <Amount value={entry.refund} className={refundOff ? 'text-rust' : ''} />
                      </td>
                      <td className="py-1.5">
                        <Amount value={entry.net_amount} />
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {orders.length ? (
        <section>
          <h3 className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-muted">
            Order record
          </h3>
          <p className="mt-1 text-xs text-ink-muted">The merchant’s own recorded amount, refund, and fee.</p>
          <div className="mt-2 overflow-x-auto">
            <table className="w-full min-w-[28rem] text-left text-sm">
              <thead>
                <tr className="border-b border-rule text-[11px] uppercase tracking-[0.14em] text-ink-muted">
                  <th className="py-1.5 pr-3 font-medium">Order</th>
                  <th className="py-1.5 pr-3 font-medium">Recorded amount</th>
                  <th className="py-1.5 pr-3 font-medium">Refund</th>
                  <th className="py-1.5 font-medium">Fee</th>
                </tr>
              </thead>
              <tbody>
                {orders.map((order) => {
                  const entry = entries.find((row) => row.order_ref === order.order_ref)
                  const refundOff =
                    classification === 'MISSING_REFUND_RECORD' &&
                    amountsDiffer(entry?.refund ?? null, order.refund_amount)
                  const feeOff =
                    classification === 'FEE_TIER_MISMATCH' &&
                    amountsDiffer(entry?.fee, order.fee_amount)
                  return (
                    <tr key={order.id} className="border-b border-rule/70 last:border-0">
                      <td className="py-1.5 pr-3 font-mono text-xs">{order.order_ref}</td>
                      <td className="py-1.5 pr-3">
                        <Amount value={order.amount} />
                      </td>
                      <td className={`py-1.5 pr-3 ${refundOff ? 'bg-rust-wash' : ''}`}>
                        <Amount value={order.refund_amount} className={refundOff ? 'text-rust' : ''} />
                      </td>
                      <td className={`py-1.5 ${feeOff ? 'bg-rust-wash' : ''}`}>
                        <Amount value={order.fee_amount} className={feeOff ? 'text-rust' : ''} />
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {banks.length ? (
        <section>
          <h3 className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-muted">
            Bank transaction
          </h3>
          <ul className="mt-2 space-y-3">
            {banks.map((txn) => (
              <li key={txn.id} className="grid gap-2 text-sm sm:grid-cols-3">
                <Field label="Amount">
                  <Amount value={txn.amount} />
                </Field>
                <Field label="Date">{formatDate(txn.date)}</Field>
                <Field label="Reference">
                  <span className="font-mono text-xs">{txn.reference}</span>
                </Field>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  )
}

function isAnomalyPayload(data) {
  return data != null && typeof data.metric === 'string' && 'batch_value' in data && !('settlement_entries' in data)
}

function AnomalyComparison({ data }) {
  if (data?.metric == null || data.batch_value == null || data.baseline_mean == null) {
    return <p className="mt-2 text-sm text-ink-muted">No comparison available for this exception.</p>
  }

  const metric = metricLabel(data.metric)
  const batchRate = formatRatePercent(data.batch_value)
  const baselineRate = formatRatePercent(data.baseline_mean)
  const fromBatches = formatBatchIds(data.baseline_batches)
  const higher = Number(data.batch_value) > Number(data.baseline_mean)
  const direction = higher ? 'higher' : 'lower'
  const relative = formatPercentPoints(data.relative_deviation_pct)
  const stdevs = Number(data.deviation_stdevs).toLocaleString('en-IN', {
    minimumFractionDigits: 1,
    maximumFractionDigits: 2,
  })

  return (
    <div className="mt-3 space-y-4">
      <dl className="grid gap-3 sm:grid-cols-2">
        <div className="rounded-sm border border-rust/40 bg-rust-wash px-4 py-3">
          <dt className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-muted">
            This batch’s {metric}
          </dt>
          <dd className="mt-1 font-serif text-3xl tracking-tight text-rust">{batchRate}</dd>
        </div>
        <div className="rounded-sm border border-rule bg-paper px-4 py-3">
          <dt className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-muted">
            Historical baseline
          </dt>
          <dd className="mt-1 font-serif text-3xl tracking-tight text-ink">{baselineRate}</dd>
          <p className="mt-1 text-xs text-ink-muted">from batches {fromBatches}</p>
        </div>
      </dl>
      <div className="rounded-sm border border-rust/40 bg-rust-wash px-4 py-3 text-rust">
        <p className="text-sm leading-snug text-ink">
          {relative} {direction} than normal — {stdevs} standard deviations from baseline
        </p>
      </div>
    </div>
  )
}

function MismatchCallout({ classification, entries, orders }) {
  const lookup = ordersByRef(orders)

  if (classification === 'MISSING_REFUND_RECORD') {
    const pairs = entries
      .map((entry) => ({
        ref: entry.order_ref,
        settlement: entry.refund,
        merchant: lookup[entry.order_ref]?.refund_amount ?? null,
      }))
      .filter((pair) => amountsDiffer(pair.settlement, pair.merchant))
    if (!pairs.length) return null
    return (
      <Callout title="The refunds do not match">
        {pairs.map((pair) => (
          <SideBySide
            key={pair.ref}
            refLabel={pair.ref}
            leftLabel="Settlement refund"
            left={pair.settlement}
            rightLabel="Merchant refund"
            right={pair.merchant}
          />
        ))}
      </Callout>
    )
  }

  if (classification === 'FEE_TIER_MISMATCH') {
    const pairs = entries
      .map((entry) => ({
        ref: entry.order_ref,
        settlement: entry.fee,
        merchant: lookup[entry.order_ref]?.fee_amount ?? null,
      }))
      .filter((pair) => amountsDiffer(pair.settlement, pair.merchant))
    if (!pairs.length) return null
    return (
      <Callout title="The fees do not match">
        {pairs.map((pair) => (
          <SideBySide
            key={pair.ref}
            refLabel={pair.ref}
            leftLabel="Settlement fee"
            left={pair.settlement}
            rightLabel="Merchant fee"
            right={pair.merchant}
          />
        ))}
      </Callout>
    )
  }

  if (classification === 'DUPLICATE_ENTRY') {
    const refs = [...new Set(entries.filter((entry) => isDuplicateRef(entries, entry.order_ref)).map((e) => e.order_ref))]
    if (!refs.length) return null
    return (
      <Callout title="The same order appears more than once">
        <p className="text-sm text-ink">
          {refs.map((ref) => (
            <span key={ref} className="mr-2 font-mono text-xs">
              {ref}
            </span>
          ))}
        </p>
      </Callout>
    )
  }

  return null
}

function Callout({ title, children }) {
  return (
    <div className="rounded-sm border border-rust/40 bg-rust-wash px-4 py-3 text-rust">
      <p className="text-[11px] font-semibold uppercase tracking-[0.14em]">{title}</p>
      <div className="mt-2 space-y-3 text-ink">{children}</div>
    </div>
  )
}

function SideBySide({ refLabel, leftLabel, left, rightLabel, right }) {
  return (
    <div>
      <p className="font-mono text-xs text-ink-muted">{refLabel}</p>
      <dl className="mt-1 grid gap-3 sm:grid-cols-2">
        <div className="rounded-sm bg-paper-raised px-3 py-2">
          <dt className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-muted">{leftLabel}</dt>
          <dd className="mt-0.5 text-base text-rust">
            <Amount value={left} className="text-rust" />
          </dd>
        </div>
        <div className="rounded-sm bg-paper-raised px-3 py-2">
          <dt className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-muted">{rightLabel}</dt>
          <dd className="mt-0.5 text-base text-rust">
            <Amount value={right} className="text-rust" />
          </dd>
        </div>
      </dl>
    </div>
  )
}

function Field({ label, children }) {
  return (
    <div>
      <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-muted">{label}</p>
      <p className="mt-0.5 text-ink">{children}</p>
    </div>
  )
}

function isDuplicateRef(entries, orderRef) {
  return entries.filter((entry) => entry.order_ref === orderRef).length > 1
}
