import { getReconStatus } from '../lib/status'

const KIND_STYLES = {
  reconciled: {
    wrap: 'border-forest bg-forest-wash text-forest',
    bar: 'bg-forest',
  },
  blocked: {
    wrap: 'border-amber bg-amber-wash text-amber',
    bar: 'bg-amber',
  },
  unmatched: {
    wrap: 'border-rust bg-rust-wash text-rust',
    bar: 'bg-rust',
  },
  variance: {
    wrap: 'border-rust bg-rust-wash text-rust',
    bar: 'bg-rust',
  },
}

export default function StatusBanner({ batch, size = 'compact' }) {
  const status = getReconStatus(batch)
  const styles = KIND_STYLES[status.kind]
  const large = size === 'full'

  return (
    <div
      className={`relative overflow-hidden rounded-sm border transition-colors duration-300 ${styles.wrap}`}
      role="status"
      aria-label={status.label}
    >
      <div className={`absolute inset-y-0 left-0 w-2 ${styles.bar}`} />
      <div className={large ? 'px-6 py-6 pl-8' : 'px-4 py-3.5 pl-6'}>
        <p
          className={`font-sans font-bold uppercase tracking-[0.22em] ${
            large ? 'text-2xl sm:text-3xl' : 'text-lg sm:text-xl'
          }`}
        >
          {status.label}
        </p>
        <p className={`mt-1.5 max-w-2xl leading-snug text-current/90 ${large ? 'text-base' : 'text-sm'}`}>
          {status.summary}
        </p>
      </div>
    </div>
  )
}
