import { createContext, useContext, useEffect, useState } from 'react'
import { getMe, getStoredToken, login as apiLogin, logout as apiLogout, setStoredToken } from '../api'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    const token = getStoredToken()
    if (!token) {
      setLoading(false)
      return
    }
    getMe()
      .then((me) => {
        if (!cancelled) setUser(me)
      })
      .catch(() => {
        // Stale/invalid token (e.g. server restarted -- sessions aren't
        // persisted across a full regen) -- clear it and fall back to login.
        setStoredToken(null)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  async function login(username, password) {
    const response = await apiLogin(username, password)
    setStoredToken(response.token)
    setUser({
      username: response.username, role: response.role,
      display_name: response.display_name, job_title: response.job_title,
    })
  }

  async function logout() {
    try {
      await apiLogout()
    } catch {
      // Log out locally regardless of whether the server call succeeded.
    }
    setStoredToken(null)
    setUser(null)
  }

  return (
    <AuthContext.Provider value={{ user, loading, login, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used within an AuthProvider')
  return context
}
