import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { api, type AuthStatus } from '@/lib/api'
import { Auth } from '@/pages/Auth'
import { AuthContext } from '@/lib/auth'
import { activateSession, AUTH_CHANGED_KEY, lockSession, migrateInitialAdminStorage, isSessionLocked, applyAccountAppearance } from '@/lib/sessionRuntime'

export function AuthBoundary({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus | null>(null)
  const [error, setError] = useState('')
  const identity = useRef<string | null>(null)
  const mountedBusiness = useRef(false)
  const revision = useRef(0)
  const accept = useCallback((next: AuthStatus) => {
    const signature = next.authenticated && next.user ? `${next.user.id}:${next.user.role}:${next.user.must_change_password}:${next.expires_at}` : null
    if ((identity.current !== null || mountedBusiness.current) && (identity.current !== signature || isSessionLocked())) {
      lockSession()
      window.location.reload()
      return
    }
    identity.current = signature
    if (next.authenticated && next.user) activateSession(next.user.id)
    if (next.authenticated && next.user && !next.user.must_change_password) {
      if (next.user.is_initial_admin) migrateInitialAdminStorage(next.user.id)
      applyAccountAppearance()
      mountedBusiness.current = true
    }
    setStatus(next)
    setError('')
  }, [])
  const check = useCallback(async () => {
    const current = ++revision.current
    try { const next = await api.authStatus(); if (current === revision.current) accept(next) }
    catch { if (!mountedBusiness.current) setError('暂时无法验证身份，请检查网络后重试。') }
  }, [accept])
  useEffect(() => {
    void check()
    const interval = window.setInterval(() => void check(), 5000)
    const locked = () => { revision.current++; setStatus({ configured: true, authenticated: false, user: null, expires_at: null }) }
    const focus = () => { if (!document.hidden) void check() }
    const storage = (event: StorageEvent) => { if (event.key === AUTH_CHANGED_KEY) { lockSession(); window.location.reload() } }
    window.addEventListener('session-locked', locked)
    window.addEventListener('session-check', check)
    window.addEventListener('focus', focus)
    window.addEventListener('storage', storage)
    document.addEventListener('visibilitychange', focus)
    return () => { clearInterval(interval); window.removeEventListener('session-locked', locked); window.removeEventListener('session-check', check); window.removeEventListener('focus', focus); window.removeEventListener('storage', storage); document.removeEventListener('visibilitychange', focus) }
  }, [check])
  useEffect(() => {
    if (!status?.authenticated || !status.expires_at) return
    const remaining = new Date(status.expires_at).getTime() - Date.now()
    if (!Number.isFinite(remaining)) return
    const timeout = window.setTimeout(lockSession, Math.max(0, remaining))
    return () => window.clearTimeout(timeout)
  }, [status?.authenticated, status?.expires_at])
  if (!status?.authenticated || !status.user || status.user.must_change_password) {
    return <Auth key={status?.user?.id ?? 'anonymous'} status={status} error={error} retry={() => void check()} onAuthenticated={next => { revision.current++; if (mountedBusiness.current) window.location.reload(); else { identity.current = null; accept(next) } }} />
  }
  return <AuthContext.Provider value={status.user}>{children}</AuthContext.Provider>
}
