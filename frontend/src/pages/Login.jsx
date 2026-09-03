import { useState } from 'react'
import { useAuth } from '../lib/AuthContext'

const DEMO_ACCOUNTS = [
  { username: 'sneha', role: 'Approver', title: 'Finance Analyst' },
  { username: 'rahul', role: 'Analyst', title: 'Reconciliation Analyst' },
  { username: 'priya', role: 'Approver', title: 'Settlements Lead' },
]

export default function Login() {
  const { login } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  async function handleSubmit(event) {
    event.preventDefault()
    setLoading(true)
    setError(null)
    try {
      await login(username.trim().toLowerCase(), password)
    } catch (err) {
      setError(err.status === 401 ? 'Invalid username or password.' : err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-paper px-4">
      <div className="w-full max-w-sm">
        <p className="text-[11px] font-semibold uppercase tracking-[0.22em] text-brass">LedgerTrail</p>
        <h1 className="mt-1 font-serif text-3xl tracking-tight text-ink">Sign in</h1>
        <p className="mt-2 text-sm text-ink-muted">
          Synthetic demo credentials — not real accounts. Two roles: an Analyst can view and
          investigate; only an Approver can approve or reject.
        </p>

        <form onSubmit={handleSubmit} className="mt-6 space-y-3 rounded-sm border border-rule bg-paper-raised p-5">
          <label className="block text-sm">
            <span className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-muted">Username</span>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              className="mt-1 w-full rounded-sm border border-rule bg-paper px-3 py-1.5 text-ink outline-none focus:border-forest"
            />
          </label>
          <label className="block text-sm">
            <span className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-muted">Password</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              className="mt-1 w-full rounded-sm border border-rule bg-paper px-3 py-1.5 text-ink outline-none focus:border-forest"
            />
          </label>

          {error ? <p className="text-sm text-rust">{error}</p> : null}

          <button
            type="submit"
            disabled={loading || !username.trim() || !password}
            className="w-full rounded-sm bg-forest px-3 py-2 text-sm font-medium text-paper-raised disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-forest"
          >
            {loading ? 'Signing in…' : 'Sign in'}
          </button>
        </form>

        <div className="mt-4 border border-dashed border-brass/70 bg-amber-wash px-3 py-2.5 text-xs leading-relaxed text-ink">
          <span className="font-semibold text-brass">Demo credentials</span> — password{' '}
          <code className="font-mono">demo1234</code> for all:
          <ul className="mt-1.5 space-y-0.5">
            {DEMO_ACCOUNTS.map((a) => (
              <li key={a.username}>
                <code className="font-mono">{a.username}</code> — {a.title} ({a.role})
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  )
}
