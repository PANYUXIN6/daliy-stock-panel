// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type AssistantToolPermissionsResponse, type User, type UserRole } from '@/lib/api'
import { AuthContext } from '@/lib/auth'
import { QK } from '@/lib/queryKeys'
import { AssistantToolPermissions } from './AssistantToolPermissions'

const mocks = vi.hoisted(() => ({ load: vi.fn(), save: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { assistantToolPermissions: mocks.load, saveAssistantToolPermissions: mocks.save } }))

const baseTools: AssistantToolPermissionsResponse['tools'] = [
  { name: 'quote', label: '股票行情', description: '查询最新股票行情', group: '基础查询', min_role: 'guest', default_min_role: 'guest', locked: false, requires_confirmation: false },
  { name: 'watchlist', label: '添加自选', description: '添加到当前用户自选股', group: '个人自选', min_role: 'guest', default_min_role: 'guest', locked: false, requires_confirmation: true },
  { name: 'strategy', label: '运行策略', description: '执行选股策略', group: '策略分析', min_role: 'pro', default_min_role: 'pro', locked: false, requires_confirmation: false },
  { name: 'backtest', label: '策略回测', description: '执行策略回测', group: '策略分析', min_role: 'admin', default_min_role: 'admin', locked: true, requires_confirmation: true },
]
function response(): AssistantToolPermissionsResponse {
  return { tools: baseTools.map(tool => ({ ...tool })), counts: { guest: 2, pro: 3, admin: 4 }, configuration_error: null }
}
function user(role: UserRole): User {
  return { id: role, username: role, role, is_initial_admin: role === 'admin', pro_expires_on: null, is_active: true, must_change_password: false, created_at: '', last_login_at: null, last_login_ip: null, login_count: 1 }
}
let host: HTMLDivElement
let root: Root
let qc: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  mocks.load.mockResolvedValue(response())
  mocks.save.mockImplementation(async (permissions: Record<string, UserRole>) => ({
    ...response(), tools: baseTools.map(tool => ({ ...tool, min_role: permissions[tool.name] })),
  }))
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  qc.clear()
  host.remove()
})
async function settle() {
  for (let index = 0; index < 3; index++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function render(identity: User | null = user('admin')) {
  await act(async () => root.render(
    <QueryClientProvider client={qc}><AuthContext.Provider value={identity}><AssistantToolPermissions /></AuthContext.Provider></QueryClientProvider>,
  ))
  await settle()
}
async function click(text: string) {
  const button = [...host.querySelectorAll('button')].find(item => item.textContent === text)!
  await act(async () => button.click())
  await settle()
}
async function select(label: string, value: UserRole) {
  const field = host.querySelector<HTMLSelectElement>(`select[aria-label="${label}最低可用角色"]`)!
  await act(async () => { field.value = value; field.dispatchEvent(new Event('change', { bubbles: true })) })
}

it('lets an admin edit inherited permissions and saves the full draft separately', async () => {
  await render()
  expect(host.querySelector('[aria-label="游客可用 2 个工具"]')).not.toBeNull()
  expect(host.querySelector('[aria-label="Pro可用 3 个工具"]')).not.toBeNull()
  expect(host.textContent).toContain('执行前需确认')
  expect(host.querySelector<HTMLSelectElement>('select[aria-label="添加自选最低可用角色"]')?.value).toBe('guest')
  expect(host.querySelector<HTMLSelectElement>('select[aria-label="策略回测最低可用角色"]')?.disabled).toBe(true)

  await select('运行策略', 'guest')
  expect(host.querySelector('[aria-label="游客可用 3 个工具"]')).not.toBeNull()
  expect(host.querySelector('[aria-label="Pro可用 3 个工具"]')).not.toBeNull()
  expect(host.querySelector('[aria-label="管理员可用 4 个工具"]')).not.toBeNull()
  expect(mocks.save).not.toHaveBeenCalled()
  await click('保存工具权限')
  expect(api.saveAssistantToolPermissions).toHaveBeenCalledWith({ quote: 'guest', watchlist: 'guest', strategy: 'guest', backtest: 'admin' })
  expect(host.textContent).toContain('工具权限已保存')
  expect(qc.getQueryData<AssistantToolPermissionsResponse>(QK.assistantToolPermissions)?.tools.find(tool => tool.name === 'strategy')?.min_role).toBe('guest')
  expect(mocks.load).toHaveBeenCalledTimes(1)
})

it('restores defaults only in the draft and preserves unsaved edits during cache refresh', async () => {
  const custom = response()
  custom.tools[0].min_role = 'pro'
  mocks.load.mockResolvedValue(custom)
  await render()
  await select('添加自选', 'pro')
  await act(async () => { qc.setQueryData(QK.assistantToolPermissions, response()) })
  expect(host.querySelector<HTMLSelectElement>('select[aria-label="添加自选最低可用角色"]')?.value).toBe('pro')
  await click('恢复默认')
  expect(host.querySelector<HTMLSelectElement>('select[aria-label="添加自选最低可用角色"]')?.value).toBe('guest')
  expect(host.querySelector<HTMLSelectElement>('select[aria-label="运行策略最低可用角色"]')?.value).toBe('pro')
  expect(host.querySelector<HTMLSelectElement>('select[aria-label="策略回测最低可用角色"]')?.value).toBe('admin')
  expect(mocks.save).not.toHaveBeenCalled()
})

it('keeps failed drafts and prevents edits or duplicate submissions while saving', async () => {
  let reject!: (reason: Error) => void
  mocks.save.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail }))
  await render()
  await select('股票行情', 'pro')
  await click('保存工具权限')
  expect([...host.querySelectorAll('select')].every(field => field.disabled)).toBe(true)
  expect(host.querySelector<HTMLButtonElement>('button:last-child')?.disabled).toBe(true)
  await act(async () => reject(new Error('配置写入失败')))
  await settle()
  expect(host.textContent).toContain('保存失败：配置写入失败')
  expect(host.querySelector<HTMLSelectElement>('select[aria-label="股票行情最低可用角色"]')?.value).toBe('pro')
  await click('保存工具权限')
  expect(host.textContent).toContain('工具权限已保存')
})

it('shows invalid persisted configuration and lets an admin explicitly save defaults to repair it', async () => {
  mocks.load.mockResolvedValue({ ...response(), configuration_error: '无法读取已保存的权限配置' })
  await render()
  expect(host.textContent).toContain('工具权限配置异常')
  await click('恢复默认')
  expect(mocks.save).not.toHaveBeenCalled()
  await click('保存工具权限')
  expect(mocks.save).toHaveBeenCalledOnce()
  expect(host.textContent).not.toContain('工具权限配置异常')
})

it('shows loading, retryable errors, and the empty state', async () => {
  let reject!: (reason: Error) => void
  mocks.load.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail }))
  await render()
  expect(host.textContent).toContain('正在加载工具权限')
  await act(async () => reject(new Error('服务暂不可用')))
  await settle()
  expect(host.textContent).toContain('工具权限加载失败：服务暂不可用')
  mocks.load.mockResolvedValue({ ...response(), tools: [] })
  await click('重新加载')
  expect(host.textContent).toContain('暂无可配置的助手工具')
})

it.each(['guest', 'pro', null] as const)('does not request or show admin settings for %s', async role => {
  await render(role ? user(role) : null)
  expect(mocks.load).not.toHaveBeenCalled()
  expect(host.textContent).toBe('')
})
