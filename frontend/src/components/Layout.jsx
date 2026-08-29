import { Link } from 'react-router-dom'

function Mark() {
  return (
    <svg viewBox="0 0 28 28" className="h-7 w-7" aria-hidden="true">
      <rect width="28" height="28" rx="5" className="fill-forest" />
      <rect x="6" y="7" width="16" height="1.5" className="fill-paper" />
      <rect x="6" y="11.5" width="12" height="1.5" className="fill-paper" />
      <rect x="6" y="16" width="16" height="1.5" className="fill-paper" />
      <rect x="6" y="20.5" width="9" height="1.5" className="fill-paper" />
    </svg>
  )
}

export default function Layout({ children }) {
  return (
    <div className="min-h-svh">
      <header className="sticky top-0 z-20 border-b border-rule/80 bg-paper-raised/90 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-5 py-3.5">
          <Link to="/" className="flex items-center gap-2.5 text-ink no-underline">
            <Mark />
            <span className="font-serif text-2xl leading-none tracking-tight">
              Ledger<i className="not-italic text-forest">Trail</i>
            </span>
          </Link>
          <p className="hidden text-sm text-ink-muted sm:block">Settlement reconciliation</p>
        </div>
      </header>
      <main className="mx-auto w-full max-w-6xl px-5 py-8">{children}</main>
    </div>
  )
}
