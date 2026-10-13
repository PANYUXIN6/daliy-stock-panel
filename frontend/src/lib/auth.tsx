import { createContext, useContext } from 'react'
import type { User } from './api'
export const AuthContext = createContext<User | null>(null)
export function useCurrentUser() { return useContext(AuthContext) }
export function AdminOnly({ children }: { children: React.ReactNode }) {
  const user = useCurrentUser()
  return user?.role === 'admin' ? children : <div role="alert" className="p-8 text-secondary">此页面仅管理员可用。</div>
}
