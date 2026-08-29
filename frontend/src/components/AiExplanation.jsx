import { useState } from 'react'
import { getExceptionExplanation } from '../api'

export default function AiExplanation({ batchId, exceptionId }) {
  const [text, setText] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  async function generate() {
    setLoading(true)
    setError(null)
    try {
      const data = await getExceptionExplanation(batchId, exceptionId)
      setText(data.explanation)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="mt-4 max-w-2xl border-t border-rule pt-3">
      <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-muted">
        AI Explanation
      </p>

      {text ? <p className="mt-2 text-sm leading-relaxed text-ink">{text}</p> : null}

      {loading ? <p className="mt-2 text-sm text-ink-muted">Writing explanation…</p> : null}

      {error ? (
        <p className="mt-2 text-sm text-rust">
          Could not load the explanation.
          <button
            type="button"
            onClick={generate}
            className="ml-2 underline"
          >
            Try again
          </button>
        </p>
      ) : null}

      {!text && !loading ? (
        <button
          type="button"
          onClick={generate}
          className="mt-2 rounded-sm border border-rule px-3 py-1.5 text-sm text-ink hover:border-ink/40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-forest"
        >
          Generate explanation
        </button>
      ) : null}
    </div>
  )
}
