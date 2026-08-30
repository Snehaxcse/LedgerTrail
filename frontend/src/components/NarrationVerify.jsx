import { useState } from 'react'
import { verifyBankNarration } from '../api'

function verificationLabel(result) {
  const consistent = result.is_settlement_credit
  const aiVerified = result.source === 'ai_verified'
  if (aiVerified && consistent) return 'AI-verified: consistent with Razorpay settlement'
  if (aiVerified && !consistent) return 'AI-verified: not a Razorpay settlement credit'
  if (consistent) return 'Consistent with Razorpay settlement (keyword check)'
  return 'Not a Razorpay settlement credit (keyword check)'
}

export default function NarrationVerify({ bankTransactionId, compact = false }) {
  const [result, setResult] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  async function verify() {
    setLoading(true)
    setError(null)
    try {
      const data = await verifyBankNarration(bankTransactionId)
      setResult(data)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const consistent = result?.is_settlement_credit
  const badgeClass = consistent
    ? 'border-forest/40 bg-forest-wash text-forest'
    : 'border-amber/40 bg-amber-wash text-amber'

  return (
    <div className={compact ? '' : 'mt-2'}>
      {result ? (
        <div>
          <p
            className={`inline-block border px-2 py-0.5 text-[11px] font-semibold leading-snug ${badgeClass}`}
          >
            {verificationLabel(result)}
          </p>
          {result.confidence_note ? (
            <p className={`text-ink-muted leading-relaxed ${compact ? 'mt-1 text-[11px]' : 'mt-1.5 text-sm'}`}>
              {result.confidence_note}
            </p>
          ) : null}
        </div>
      ) : null}

      {loading ? <p className="text-sm text-ink-muted">Checking narration…</p> : null}

      {error ? (
        <p className="text-sm text-rust">
          Could not verify this narration.
          <button type="button" onClick={verify} className="ml-2 underline">
            Try again
          </button>
        </p>
      ) : null}

      {!result && !loading ? (
        <button
          type="button"
          onClick={verify}
          className="rounded-sm border border-rule px-3 py-1.5 text-sm text-ink hover:border-ink/40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-forest"
        >
          Verify narration
        </button>
      ) : null}
    </div>
  )
}
