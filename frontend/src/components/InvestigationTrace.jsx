import { useState } from 'react'
import { investigateException } from '../api'

const STATUS_CONFIG = {
  VERIFIED_EXPLANATION: {
    label: 'Verified explanation',
    icon: '✓',
    wrap: 'border-forest bg-forest-wash text-forest',
    bar: 'bg-forest',
    summary: 'Every claim below was checked against the underlying settlement, order, and bank records.',
  },
  PARTIALLY_VERIFIED: {
    label: 'Partially verified',
    icon: '⚠',
    wrap: 'border-amber bg-amber-wash text-amber',
    bar: 'bg-amber',
    summary:
      'Some claims were confirmed against source records. Others are the AI’s own interpretation and were not independently verified.',
  },
  INSUFFICIENT_EVIDENCE: {
    label: 'Insufficient evidence',
    icon: '⚠',
    wrap: 'border-amber bg-amber-wash text-amber',
    bar: 'bg-amber',
    summary:
      'The AI could not reach a confident conclusion from the available records. Nothing below should be treated as an explanation.',
  },
  CONTRADICTED: {
    label: 'AI interpretation rejected',
    icon: '✕',
    wrap: 'border-rust bg-rust-wash text-rust',
    bar: 'bg-rust',
    summary:
      'A deterministic check found a claim that does not match the source records. This hypothesis was rejected — treat nothing below as fact.',
  },
  HUMAN_REVIEW_REQUIRED: {
    label: 'Human review required',
    icon: '⚠',
    wrap: 'border-rust bg-rust-wash text-rust',
    bar: 'bg-rust',
    summary:
      'This investigation could not produce a reliable report and was discarded by a safety check. Review this exception manually.',
  },
}

function formatToolName(name) {
  const spaced = String(name || '').replaceAll('_', ' ')
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

function summarizeInput(input) {
  if (!input || typeof input !== 'object') return ''
  const entries = Object.entries(input)
  if (!entries.length) return ''
  return entries.map(([key, value]) => `${key}: ${value}`).join(', ')
}

function isVerifierRejection(text) {
  return typeof text === 'string' && text.includes('[REJECTED BY VERIFIER')
}

// The malformed-shape safety check returns its own explanation through the
// same "contradictions" slot the AI's self-reported disagreements use. It is
// a verifier message, not something the AI said, and must not be attributed
// to the AI in the UI -- that would blur exactly the boundary this component
// exists to keep clear.
function isMalformedShapeNotice(text) {
  return typeof text === 'string' && text.startsWith('Investigation report malformed:')
}

export default function InvestigationTrace({ batchId, exceptionId, fetcher, buttonLabel = 'Investigate with AI' }) {
  const [result, setResult] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  async function run() {
    setLoading(true)
    setError(null)
    try {
      const data = fetcher ? await fetcher() : await investigateException(batchId, exceptionId)
      setResult(data)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="mt-4 max-w-3xl border-t border-rule pt-3">
      <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-muted">
        AI Investigation
      </p>

      {!result && !loading ? (
        <button
          type="button"
          onClick={run}
          className="mt-2 rounded-sm border border-rule px-3 py-1.5 text-sm text-ink hover:border-ink/40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-forest"
        >
          {buttonLabel}
        </button>
      ) : null}

      {loading ? <p className="mt-2 text-sm text-ink-muted">Investigating — running tools and checking the result…</p> : null}

      {error ? (
        <p className="mt-2 text-sm text-rust">
          Could not run the investigation.
          <button type="button" onClick={run} className="ml-2 underline">
            Try again
          </button>
        </p>
      ) : null}

      {result ? <InvestigationReport result={result} /> : null}
    </div>
  )
}

function InvestigationReport({ result }) {
  const status = STATUS_CONFIG[result.investigation_status] || STATUS_CONFIG.HUMAN_REVIEW_REQUIRED
  const isFallback = result.source === 'fallback'
  const isRejected = result.investigation_status === 'CONTRADICTED'

  const malformedShapeNotices = (result.contradictions || []).filter(isMalformedShapeNotice)
  const verifierRejections = (result.contradictions || []).filter(
    (c) => isVerifierRejection(c) && !isMalformedShapeNotice(c),
  )
  const aiReportedContradictions = (result.contradictions || []).filter(
    (c) => !isVerifierRejection(c) && !isMalformedShapeNotice(c),
  )

  return (
    <div className="mt-3 space-y-4">
      <div className={`relative overflow-hidden rounded-sm border ${status.wrap}`} role="status">
        <div className={`absolute inset-y-0 left-0 w-1.5 ${status.bar}`} />
        <div className="px-4 py-3 pl-6">
          <p className="flex items-center gap-2 text-sm font-bold uppercase tracking-[0.14em]">
            <span aria-hidden="true">{status.icon}</span>
            {status.label}
            {result.cached ? (
              <span className="rounded-sm bg-ink/10 px-1.5 py-0.5 text-[10px] font-medium normal-case tracking-normal text-ink-muted">
                cached
              </span>
            ) : null}
          </p>
          <p className="mt-1 text-sm leading-snug text-current/90">{status.summary}</p>
          {isFallback ? (
            <p className="mt-1.5 text-xs leading-snug text-current/80">{result.confidence_basis}</p>
          ) : null}
          {malformedShapeNotices.length ? (
            <p className="mt-1.5 text-xs leading-snug text-current/80">
              Safety check reason: {malformedShapeNotices.join(' ')}
            </p>
          ) : null}
        </div>
      </div>

      {result.tool_calls?.length ? <InvestigationTraceSteps toolCalls={result.tool_calls} /> : null}

      {result.verified_facts?.length ? (
        <TrustSection
          tone="forest"
          icon="✓"
          title="Verified facts"
          subtitle="Confirmed against source data — safe to treat as fact."
        >
          <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-ink">
            {result.verified_facts.map((fact, i) => (
              <li key={i}>{fact}</li>
            ))}
          </ul>
        </TrustSection>
      ) : null}

      {verifierRejections.length ? (
        <TrustSection
          tone="rust"
          icon="✕"
          title="Rejected by verification"
          subtitle="These claims cited a number that no source record supports. Discarded, not shown as fact."
        >
          <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-ink">
            {verifierRejections.map((claim, i) => (
              <li key={i}>{claim.replace(/\s*\[REJECTED BY VERIFIER:[^\]]*\]/, '')}</li>
            ))}
          </ul>
        </TrustSection>
      ) : null}

      {!isFallback && (result.hypothesis || result.possible_root_cause || result.unverified_claims?.length) ? (
        <TrustSection
          tone={isRejected ? 'rust' : 'amber'}
          icon="⚠"
          title={isRejected ? 'AI hypothesis — rejected' : 'AI hypothesis — not verified'}
          subtitle="This is the AI's own interpretation. It has not been independently confirmed — do not treat it as settled financial fact."
        >
          <div className="mt-2 space-y-2 text-sm text-ink">
            {result.hypothesis ? <p className="leading-relaxed">{result.hypothesis}</p> : null}
            {result.possible_root_cause ? (
              <p className="leading-relaxed">
                <span className="font-semibold">Possible root cause: </span>
                {result.possible_root_cause}
              </p>
            ) : null}
            {result.confidence_basis ? (
              <p className="text-xs leading-relaxed text-ink-muted">{result.confidence_basis}</p>
            ) : null}
            {result.unverified_claims?.length ? (
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-muted">
                  Unverified claims
                </p>
                <ul className="mt-1 list-disc space-y-1 pl-5">
                  {result.unverified_claims.map((claim, i) => (
                    <li key={i}>{claim}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {aiReportedContradictions.length ? (
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-muted">
                  Disagreements the AI noted
                </p>
                <ul className="mt-1 list-disc space-y-1 pl-5">
                  {aiReportedContradictions.map((claim, i) => (
                    <li key={i}>{claim}</li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        </TrustSection>
      ) : null}

      {result.recommended_next_step ? (
        <TrustSection
          tone="brass"
          icon="→"
          title="Recommended next step"
          subtitle="Suggested by the AI — a human still has to decide and act."
        >
          <p className="mt-2 text-sm leading-relaxed text-ink">{result.recommended_next_step}</p>
        </TrustSection>
      ) : null}

      {!isFallback && result.ai_self_reported_status && result.ai_self_reported_status !== result.investigation_status ? (
        <p className="text-xs text-ink-muted">
          The AI itself reported this investigation as “{result.ai_self_reported_status}”. Verification
          overrode that to “{result.investigation_status}” — the AI’s own assessment is never taken as
          final.
        </p>
      ) : null}
    </div>
  )
}

const TONE_STYLES = {
  forest: 'border-forest/40 bg-forest-wash text-forest',
  amber: 'border-amber/50 border-dashed bg-amber-wash text-amber',
  rust: 'border-rust/50 bg-rust-wash text-rust',
  brass: 'border-brass/50 bg-amber-wash/40 text-brass',
}

function TrustSection({ tone, icon, title, subtitle, children }) {
  return (
    <div className={`rounded-sm border px-4 py-3 ${TONE_STYLES[tone]}`}>
      <p className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.14em]">
        <span aria-hidden="true">{icon}</span>
        {title}
      </p>
      {subtitle ? <p className="mt-1 text-xs leading-snug text-current/80">{subtitle}</p> : null}
      <div className="text-ink">{children}</div>
    </div>
  )
}

function InvestigationTraceSteps({ toolCalls }) {
  const [open, setOpen] = useState(false)

  return (
    <div className="rounded-sm border border-rule bg-paper px-4 py-3">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between text-left"
      >
        <span className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-muted">
          Investigation trace — {toolCalls.length} tool call{toolCalls.length === 1 ? '' : 's'}
        </span>
        <span className="text-xs text-ink-muted">{open ? 'Hide' : 'Show'}</span>
      </button>

      {open ? (
        <ol className="mt-3 space-y-2">
          {toolCalls.map((call, i) => (
            <li key={i} className="border-t border-rule/70 pt-2 first:border-0 first:pt-0">
              <details>
                <summary className="cursor-pointer text-sm text-ink">
                  <span className="mr-2 font-mono text-xs text-ink-muted">{i + 1}.</span>
                  <span className="font-medium">{formatToolName(call.tool)}</span>
                  {summarizeInput(call.input) ? (
                    <span className="ml-1 text-xs text-ink-muted">({summarizeInput(call.input)})</span>
                  ) : null}
                </summary>
                <pre className="mt-2 max-h-64 overflow-auto rounded-sm bg-ink/5 p-2 text-[11px] leading-snug text-ink-muted">
                  {JSON.stringify(call.result, null, 2)}
                </pre>
              </details>
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  )
}
