// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { SettingsUsersPanel } from './Users'

const mocks = vi.hoisted(() => ({ list: vi.fn(), events: vi.fn(), create: vi.fn(), update: vi.fn(), reset: vi.fn(), revoke: vi.fn(), toast: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { users: mocks.list, loginEvents: mocks.events, createUser: mocks.create, updateUser: mocks.update, resetUserPassword: mocks.reset, revokeUserSession: mocks.revoke } }))
vi.mock('@/components/Toast', () => ({ toast: mocks.toast }))
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
afterEach(async () => { await act(async () => root.unmount()); qc.clear(); host.remove(); vi.useRealTimers() })
async function settle() { for (let i = 0; i < 3; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) }) }
async function render() { await act(async () => root.render(<QueryClientProvider client={qc}><SettingsUsersPanel /></QueryClientProvider>)); await settle() }
async function click(text: string) { const button = [...host.querySelectorAll('button')].find(b => b.textContent === text)!; await act(async () => button.click()); await settle() }
async function fill(label: string, value: string) { const e = host.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!; await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(e, value); e.dispatchEvent(new Event('input', { bubbles: true })) }) }
async function submit() { await act(async () => host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))); await settle() }
async function selectRole(value: string) { const role = host.querySelector<HTMLSelectElement>('select[aria-label="角色"]')!; await act(async () => { role.value = value; role.dispatchEvent(new Event('change', { bubbles: true })) }) }
it('creates a guest with a temporary password and clears modal data after success', async () => {
  await render(); await click('创建用户')
  expect(host.querySelector<HTMLSelectElement>('select[aria-label="角色"]')!.value).toBe('guest')
  expect([...host.querySelectorAll('select[aria-label="角色"] option')].map(option => option.getAttribute('value'))).toEqual(['guest', 'pro'])
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
  expect([...role.options].map(option => option.value)).toEqual(['guest', 'pro'])
  await act(async () => { role.value = 'pro'; role.dispatchEvent(new Event('change', { bubbles: true })) })
  const date = host.querySelector<HTMLInputElement>('input[aria-label="Pro 到期日"]')!
  expect(date.required).toBe(true); expect(date.checkValidity()).toBe(false)
  await fill('Pro 到期日', '2026-12-31'); await submit()
  expect(mocks.update).toHaveBeenCalledWith('a', { role: 'pro', pro_expires_on: '2026-12-31' })
})
it.each([
  ['禁用', '确认禁用', 'update'],
  ['强制下线', '确认下线', 'revoke'],
] as const)('requires confirmation for %s and allows cancellation and retry', async (action, confirm, method) => {
  await render(); await click(action)
  expect(host.querySelector('[role="dialog"]')?.textContent).toContain('alice')
  expect(mocks[method]).not.toHaveBeenCalled()
  await click('取消')
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  expect(mocks[method]).not.toHaveBeenCalled()
  expect(mocks.toast).not.toHaveBeenCalled()
  await click(action)
  mocks[method].mockRejectedValueOnce(new Error('操作失败，请重试'))
  await click(confirm)
  expect(host.querySelector('[role="dialog"]')?.textContent).toContain('操作失败，请重试')
  expect(mocks.toast).not.toHaveBeenCalled()
  await click(confirm)
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  if (method === 'update') expect(mocks.update).toHaveBeenCalledWith('a', { is_active: false })
  else {
    expect(mocks.revoke).toHaveBeenCalledWith('a')
    expect(mocks.toast).toHaveBeenCalledExactlyOnceWith('已将用户「alice」强制下线', 'success')
  }
})
it('keeps the confirmation open and prevents duplicate requests while pending', async () => {
  let resolve!: (value: unknown) => void
  mocks.revoke.mockImplementationOnce(() => new Promise(done => { resolve = done }))
  await render(); await click('强制下线'); await click('确认下线')
  const buttons = [...host.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')]
  expect(buttons.every(button => button.disabled)).toBe(true)
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(host.querySelector('[role="dialog"]')).not.toBeNull()
  expect(mocks.revoke).toHaveBeenCalledTimes(1)
  expect(mocks.toast).not.toHaveBeenCalled()
  await act(async () => resolve({ ok: true })); await settle()
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  expect(mocks.toast).toHaveBeenCalledExactlyOnceWith('已将用户「alice」强制下线', 'success')
})
it('supports password reset while showing service errors', async () => {
  await render()
  await click('重置密码'); await fill('临时密码', 'new-temp-password')
  mocks.reset.mockRejectedValueOnce(new Error('不能重置此账号'))
  await submit(); expect(host.textContent).toContain('不能重置此账号')
  expect(mocks.reset).toHaveBeenCalledWith('a', 'new-temp-password')
})
it.each([
  ['2026-10-18T16:30:00Z', '2026-11-18'],
  ['2028-01-31T04:00:00Z', '2028-03-01'],
])('defaults new pro users to 30 days after Beijing today at %s', async (now, expected) => {
  vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(new Date(now))
  await render(); await click('创建用户'); await selectRole('pro')
  expect(host.querySelector<HTMLInputElement>('input[aria-label="Pro 到期日"]')!.value).toBe(expected)
  await fill('用户名', 'new-pro'); await fill('临时密码', 'temp-password'); await submit()
  expect(mocks.create).toHaveBeenCalledWith({ username: 'new-pro', password: 'temp-password', role: 'pro', pro_expires_on: expected })
})
it('preserves a manually selected expiry when switching roles and resets new form data on reopen', async () => {
  vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(new Date('2026-10-18T04:00:00Z'))
  await render(); await click('创建用户'); await selectRole('pro'); await fill('Pro 到期日', '2026-12-31')
  await selectRole('guest'); await selectRole('pro')
  expect(host.querySelector<HTMLInputElement>('input[aria-label="Pro 到期日"]')!.value).toBe('2026-12-31')
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  await click('创建用户'); await selectRole('pro')
  expect(host.querySelector<HTMLInputElement>('input[aria-label="Pro 到期日"]')!.value).toBe('2026-11-17')
})
it('only allows viewing login history for the admin account', async () => {
  const admin = { ...alice, id: 'admin-id', username: 'custom-admin', role: 'admin' }
  mocks.list.mockResolvedValue({ users: [admin, alice], total: 2 })
  await render()
  const rows = [...host.querySelectorAll('tbody tr')]
  const adminRow = rows.find(row => row.textContent?.includes('custom-admin'))!
  const guestRow = rows.find(row => row.textContent?.includes('alice'))!
  expect([...adminRow.querySelectorAll('button')].map(button => button.textContent)).toEqual(['登录记录'])
  expect(adminRow.textContent).toContain('管理员账号受保护')
  expect([...guestRow.querySelectorAll('button')].map(button => button.textContent)).toEqual(['编辑权限', '禁用', '重置密码', '强制下线', '登录记录'])
  await act(async () => adminRow.querySelector<HTMLButtonElement>('button')!.click())
  await settle()
  expect(mocks.events).toHaveBeenLastCalledWith('admin-id', 0)
})
