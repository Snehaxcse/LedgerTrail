import { useEffect } from 'react'

const DISMISS_MS = 6000

export default function SimulatedNotice({ notice, onDismiss }) {
  useEffect(() => {
    if (!notice) return undefined
    const timer = window.setTimeout(onDismiss, DISMISS_MS)
    return () => window.clearTimeout(timer)
  }, [notice, onDismiss])

  if (!notice) return null

  const body =
    notice.kind === 'approved'
      ? `Simulated: notification sent to finance team confirming approval of ${notice.classification}.`
      : `Simulated: notification sent to Razorpay support flagging rejected ${notice.classification} for review.`

  return (
    <div
      role="status"
      className="fixed right-5 bottom-5 z-40 w-[min(28rem,calc(100vw-2.5rem))] border-2 border-dashed border-brass bg-amber-wash px-4 py-3 shadow-[0_8px_24px_rgba(22,20,16,0.12)]"
    >
      <div className="flex items-start justify-between gap-3">
        <p className="font-serif text-2xl leading-none tracking-tight text-brass">Simulated</p>
        <button
          type="button"
          onClick={onDismiss}
          aria-label="Dismiss notification"
          className="text-ink-muted hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-forest"
        >
          ×
        </button>
      </div>
      <p className="mt-1 text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-muted">
        No real notification sent
      </p>
      <p className="mt-2 text-sm leading-snug text-ink">{body}</p>
    </div>
  )
}
