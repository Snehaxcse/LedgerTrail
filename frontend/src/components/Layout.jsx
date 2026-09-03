import { Link, NavLink } from 'react-router-dom'
import { useAuth } from '../lib/AuthContext'

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

function navClass({ isActive }) {
  return `text-sm no-underline ${
    isActive ? 'font-semibold text-ink' : 'text-ink-muted hover:text-ink'
  }`
}

function AccountBadge() {
  const { user, logout } = useAuth()
  if (!user) return null
  return (
    <div className="flex items-center gap-2 text-xs text-ink-muted">
      <span>
        <span className="font-semibold text-ink">{user.display_name}</span> — {user.job_title}{' '}
        <span className="rounded-sm bg-rule/60 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-[0.1em] text-ink">
          {user.role}
        </span>
      </span>
      <button
        type="button"
        onClick={logout}
        className="text-brass underline hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-forest"
      >
        Sign out
      </button>
    </div>
  )
}

export default function Layout({ children }) {
  return (
    <div className="min-h-svh">
      <header className="sticky top-0 z-20 border-b border-rule/80 bg-paper-raised/90 backdrop-blur">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-x-4 gap-y-2 px-5 py-3.5">
          <Link to="/" className="flex items-center gap-2.5 text-ink no-underline">
            <Mark />
            <span className="font-serif text-2xl leading-none tracking-tight">
              Ledger<i className="not-italic text-forest">Trail</i>
            </span>
          </Link>
          <nav className="flex flex-wrap items-center gap-x-5 gap-y-1">
            <NavLink to="/" end className={navClass}>
              Overview
            </NavLink>
            <NavLink to="/" end className={navClass}>
              Batches
            </NavLink>
            <NavLink to="/bank-statement" className={navClass}>
              Bank statement
            </NavLink>
            <NavLink to="/audit" className={navClass}>
              Audit trail
            </NavLink>
            <NavLink to="/transparency" className={navClass}>
              Transparency
            </NavLink>
            {/* "Demos" grouping (Agent demo / Ingestion demo / Over time) left flat
                rather than collapsed into a dropdown -- this close to submission, a
                dropdown's extra state/focus/click-outside handling isn't worth the
                risk for a purely cosmetic grouping. */}
            <NavLink to="/agent-demo" className={navClass}>
              Agent demo
            </NavLink>
            <NavLink to="/ingestion-demo" className={navClass}>
              Ingestion demo
            </NavLink>
            <NavLink to="/trend" className={navClass}>
              Over time
            </NavLink>
          </nav>
          <AccountBadge />
        </div>
      </header>
      <main className="mx-auto w-full max-w-6xl px-5 py-8">{children}</main>
      <footer className="border-t border-rule/80">
        <p className="mx-auto max-w-6xl px-5 py-4 text-xs leading-relaxed text-ink-muted">
          This demo uses SQLite for simplicity; a production deployment would use PostgreSQL
          for concurrent finance operations.
        </p>
      </footer>
    </div>
  )
}
