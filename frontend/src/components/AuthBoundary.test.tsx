// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { AuthBoundary } from './AuthBoundary'
import { AuthContext, AdminOnly } from '@/lib/auth'
import { activateSession, isSessionLocked, lockSession, sessionFetch } from '@/lib/sessionRuntime'
import type { AuthStatus, User } from '@/lib/api'

const mocks = vi.hoisted(() => ({ status: vi.fn(), login: vi.fn(), change: vi.fn(), logout: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { authStatus: mocks.status, authLogin: mocks.login, authChangePassword: mocks.change, authLogout: mocks.logout } }))
const user: User = { id: 'user-a', username: 'alice', role: 'guest', is_initial_admin: false, pro_expires_on: null, is_active: true, must_change_password: false, created_at: '', last_login_at: null, last_login_ip: null, login_count: 0 }
const anonymous: AuthStatus = { configured: true, authenticated: false, user: null, expires_at: null }
const authenticated = (change = false): AuthStatus => ({ configured: true, authenticated: true, user: { ...user, must_change_password: change }, expires_at: new Date(Date.now() + 86400000).toISOString() })
let host: HTMLDivElement
let root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  activateSession('test'); localStorage.clear(); vi.clearAllMocks()
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals() })
async function render() { await act(async () => root.render(<AuthBoundary><div data-testid="business">个人业务内容</div></AuthBoundary>)) }
async function fill(label: string, value: string) {
  const element = host.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!
  await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(element, value); element.dispatchEvent(new Event('input', { bubbles: true })) })
}
it('does not mount business while identity is unknown or cannot be verified', async () => {
  let reject!: (reason: Error) => void
  mocks.status.mockImplementation(() => new Promise((_resolve, r) => { reject = r }))
  await render()
  expect(host.textContent).toContain('正在验证身份')
  expect(host.querySelector('[data-testid="business"]')).toBeNull()
  await act(async () => reject(new Error('network unavailable')))
  expect(host.textContent).toContain('暂时无法验证身份')
  expect(host.querySelector('form')).toBeNull()
  expect(host.querySelector('[data-testid="business"]')).toBeNull()
})
it('requires account credentials and keeps the requested URL with a non-dismissible login overlay', async () => {
  window.history.replaceState(null, '', '/watchlist?group=one')
  mocks.status.mockResolvedValue(anonymous)
  mocks.login.mockResolvedValue(authenticated())
  await render()
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(host.querySelector('[role="dialog"]')).not.toBeNull()
  expect(host.querySelector('[data-testid="business"]')).toBeNull()
  await fill('用户名', 'alice'); await fill('密码', 'password-123')
  await act(async () => host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
  expect(mocks.login).toHaveBeenCalledWith('alice', 'password-123')
  expect(host.textContent).toContain('个人业务内容')
  expect(window.location.pathname + window.location.search).toBe('/watchlist?group=one')
})
it('temporary credentials cannot mount business before a successful password change', async () => {
  mocks.status.mockResolvedValue(authenticated(true))
  mocks.change.mockResolvedValue(authenticated())
  await render()
  expect(host.textContent).toContain('设置个人密码')
  expect(host.querySelector('[data-testid="business"]')).toBeNull()
  await fill('当前临时密码', 'temp-pass'); await fill('新密码', 'new-password'); await fill('确认新密码', 'different')
  await act(async () => host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
  expect(mocks.change).not.toHaveBeenCalled()
  expect(host.textContent).toContain('两次新密码不一致')
  await fill('确认新密码', 'new-password')
  await act(async () => host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
  expect(mocks.change).toHaveBeenCalledWith('temp-pass', 'new-password')
  expect(host.textContent).toContain('个人业务内容')
})
it('pins mutations to the confirmed temporary-password account before mounting business', async () => {
  mocks.status.mockResolvedValue(authenticated(true))
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response('{}'))
  vi.stubGlobal('fetch', fetchMock)
  await render()
  expect(host.querySelector('[data-testid="business"]')).toBeNull()
  const response = await sessionFetch('/api/auth/change-password', { method: 'POST' })
  await response.text()
  expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get('X-Expected-User')).toBe(user.id)
})
it('locks and discards the temporary-password form when a later check finds another account', async () => {
  mocks.status.mockResolvedValue(authenticated(true))
  await render()
  await fill('当前临时密码', 'temporary-a')
  await fill('新密码', 'new-password-a')
  mocks.status.mockResolvedValue({ ...authenticated(true), user: { ...user, id: 'user-b', username: 'bob', must_change_password: true } })
  await act(async () => window.dispatchEvent(new Event('focus')))
  expect(isSessionLocked()).toBe(true)
  expect(host.querySelector('input[aria-label="当前临时密码"]')).toBeNull()
  expect(host.querySelector<HTMLInputElement>('input[aria-label="密码"]')?.value).toBe('')
  expect(mocks.change).not.toHaveBeenCalled()
})
it('unmounts private content immediately when any transport locks the session', async () => {
  mocks.status.mockResolvedValue(authenticated())
  await render()
  expect(host.textContent).toContain('个人业务内容')
  await act(async () => lockSession())
  expect(host.querySelector('[data-testid="business"]')).toBeNull()
  expect(host.textContent).toContain('登录 Tick Stock Panel')
})
it('does not expose a browser setup form when the server is unconfigured', async () => {
  mocks.status.mockResolvedValue({ ...anonymous, configured: false })
  await render()
  expect(host.textContent).toContain('服务器尚未初始化管理员账号')
  expect(host.querySelector('form')).toBeNull()
})
it('a direct admin page does not mount its content for guest', async () => {
  await act(async () => root.render(<AuthContext.Provider value={user}><AdminOnly><div data-testid="system">系统密钥</div></AdminOnly></AuthContext.Provider>))
  expect(host.querySelector('[data-testid="system"]')).toBeNull()
  expect(host.textContent).toContain('仅管理员')
})
