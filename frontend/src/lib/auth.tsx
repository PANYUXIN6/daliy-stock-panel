import { createContext, useContext } from 'react'
import type { User } from './api'
export const AuthContext = createContext<User | null>(null)
export function useCurrentUser() { return useContext(AuthContext) }
const ADMIN_PAGES = new Set(['/data', '/dev', '/backtest', '/monitor', '/signals', '/lots'])
export function canAccessPage(user: User | null, path: string) {
  return user?.role === 'admin' || !ADMIN_PAGES.has(path)
}
export function AdminOnly({ children }: { children: React.ReactNode }) {
  const user = useCurrentUser()
  return user?.role === 'admin' ? children : <div role="alert" className="p-8 text-secondary">此页面仅管理员可用。</div>
}
