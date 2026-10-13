// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { SettingsUsersPanel } from './Users'

const mocks = vi.hoisted(() => ({ list: vi.fn(), events: vi.fn(), create: vi.fn(), update: vi.fn(), reset: vi.fn(), revoke: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { users: mocks.list, loginEvents: mocks.events, createUser: mocks.create, updateUser: mocks.update, resetUserPassword: mocks.reset, revokeUserSession: mocks.revoke } }))
const alice = { id: 'a', username: 'alice', role: 'guest', pro_expires_on: null, is_active: true, must_change_password: false, login_count: 9, last_login_at: null, last_login_ip: null }
let host: HTMLDivElement; let root: Root; let qc: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  mocks.list.mockResolvedValue({ users: [alice], total: 1 }); mocks.events.mockResolvedValue({ events: [], total: 0 })
  mocks.create.mockResolvedValue({ user: alice }); mocks.update.mockResolvedValue({ user: alice }); mocks.reset.mockResolvedValue({ ok: true }); mocks.revoke.mockResolvedValue({ ok: true })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(async () => { await act(async () => root.unmount()); qc.clear(); host.remove() })
async function settle() { for (let i = 0; i < 3; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) }) }
async function render() { await act(async () => root.render(<QueryClientProvider client={qc}><SettingsUsersPanel /></QueryClientProvider>)); await settle() }
async function click(text: string) { const button = [...host.querySelectorAll('button')].find(b => b.textContent === text)!; await act(async () => button.click()); await settle() }
async function fill(label: string, value: string) { const e = host.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!; await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(e, value); e.dispatchEvent(new Event('input', { bubbles: true })) }) }
async function submit() { await act(async () => host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))); await settle() }
it('creates a guest with a temporary password and clears modal data after success', async () => {
  await render(); await click('创建用户')
  expect(host.querySelector<HTMLSelectElement>('select[aria-label="角色"]')!.value).toBe('guest')
  await fill('用户名', 'new-guest'); await fill('临时密码', 'temp-password')
  await submit()
  expect(mocks.create).toHaveBeenCalledWith({ username: 'new-guest', password: 'temp-password', role: 'guest', pro_expires_on: null })
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  await click('创建用户')
  expect(host.querySelector<HTMLInputElement>('input[aria-label="临时密码"]')!.value).toBe('')
})
it('requires a pro expiry date and submits the selected date unchanged', async () => {
  await render(); await click('编辑权限')
  const role = host.querySelector<HTMLSelectElement>('select[aria-label="角色"]')!
  await act(async () => { role.value = 'pro'; role.dispatchEvent(new Event('change', { bubbles: true })) })
  const date = host.querySelector<HTMLInputElement>('input[aria-label="Pro 到期日"]')!
  expect(date.required).toBe(true); expect(date.checkValidity()).toBe(false)
  await fill('Pro 到期日', '2026-12-31'); await submit()
  expect(mocks.update).toHaveBeenCalledWith('a', { role: 'pro', pro_expires_on: '2026-12-31' })
})
it('supports disabling, forced logout and password reset while showing service errors', async () => {
  await render(); await click('禁用')
  expect(mocks.update).toHaveBeenCalledWith('a', { is_active: false })
  await click('强制下线'); expect(mocks.revoke).toHaveBeenCalledWith('a')
  await click('重置密码'); await fill('临时密码', 'new-temp-password')
  mocks.reset.mockRejectedValueOnce(new Error('不能重置此账号'))
  await submit(); expect(host.textContent).toContain('不能重置此账号')
  expect(mocks.reset).toHaveBeenCalledWith('a', 'new-temp-password')
})
