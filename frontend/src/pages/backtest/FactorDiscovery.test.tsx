// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type FactorBatchItem, type User } from '@/lib/api'
import { AuthContext } from '@/lib/auth'
import { QK } from '@/lib/queryKeys'
import { FactorDiscovery } from './FactorDiscovery'

vi.mock('./AutoMiningDialog', () => ({ AutoMiningDialog: () => null }))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

const item: FactorBatchItem = {
  factor_name: 'rsi_14', label: '相对强弱', group: '超买超卖',
  ic_mean: -0.04, ir: -0.5, ic_win_rate: 0.6,
  long_short_return: 0.08, long_short_max_drawdown: -0.03,
  n_symbols: 100, n_dates: 60, elapsed_ms: 12, error: null,
}
const user: User = {
  id: 'factor-user', username: 'factor-user', role: 'admin', is_initial_admin: false,
  pro_expires_on: null, is_active: true, must_change_password: false,
  created_at: '', last_login_at: null, last_login_ip: null, login_count: 0,
}
let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(api, 'factorColumns').mockResolvedValue({
    columns: [{ id: item.factor_name, label: item.label, group: item.group, desc: '14 日 RSI' }],
  })
  vi.spyOn(api, 'watchlistList').mockResolvedValue({ symbols: [] })
  vi.spyOn(api, 'factorBatch').mockResolvedValue({
    run_id: 'batch-1', config: { end: '2026-10-16' }, results: [item],
    elapsed_ms: 12, n_symbols: 100, n_dates: 60, error: null,
  })
  vi.spyOn(api, 'factorRun').mockResolvedValue({
    run_id: 'single-1', config: { factor_name: item.factor_name },
    ic_mean: item.ic_mean, ic_std: 0.08, ir: item.ir, ic_win_rate: item.ic_win_rate,
    ic_series: [], group_stats: [], group_nav: [], long_short_stats: {}, long_short_nav: [],
    elapsed_ms: 12, n_symbols: 100, n_dates: 60, error: null,
  })
  vi.spyOn(api, 'researchCandidateCreate').mockImplementation(async candidate => ({
    ...candidate, id: 'candidate-1', status: 'pending', data_as_of: candidate.data_as_of ?? null,
    created_at: '', updated_at: '',
  }))
  vi.spyOn(api, 'customSignalSave').mockImplementation(async signal => ({ ok: true, signal }))
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.restoreAllMocks()
})

async function settle() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 15)) })
}
async function render(role: User['role']) {
  await act(async () => root.render(
    <AuthContext.Provider value={{ ...user, role }}><QueryClientProvider client={client}>
      <FactorDiscovery focusFactor={item.factor_name} />
    </QueryClientProvider></AuthContext.Provider>,
  ))
  await settle()
}
function button(label: string) {
  return [...host.querySelectorAll('button')].find(value => value.textContent?.trim() === label)
}
function signalButton() {
  return host.querySelector<HTMLButtonElement>(`[aria-label="把 ${item.label} 加入信号条件"]`)
}
async function runBatch(role: User['role']) {
  await render(role)
  expect(button('筛选 1 个因子')?.disabled).toBe(false)
  await act(async () => button('筛选 1 个因子')!.click())
  await settle()
  expect(api.factorBatch).toHaveBeenCalledWith(expect.objectContaining({ factor_names: [item.factor_name] }))
  expect(host.textContent).toContain('筛选结果')
}

it.each(['guest', 'pro'] as const)('%s 可筛选、保存候选并检验因子，无创建信号入口', async role => {
  await runBatch(role)
  expect(host.textContent).toContain('100 只标的')
  expect(signalButton()).toBeNull()
  expect(host.querySelector('[role="dialog"]')).toBeNull()

  await act(async () => host.querySelector<HTMLButtonElement>(`[aria-label="保存 ${item.label} 为候选"]`)!.click())
  await settle()
  expect(api.researchCandidateCreate).toHaveBeenCalledWith(expect.objectContaining({
    kind: 'factor', source_id: item.factor_name, metrics: expect.objectContaining({ ic_mean: item.ic_mean }),
  }))

  await act(async () => host.querySelector<HTMLButtonElement>(`[aria-label="查看 ${item.label} 的单因子检验"]`)!.click())
  await act(async () => button('开始因子分析')!.click())
  await settle()
  expect(api.factorRun).toHaveBeenCalledWith(expect.objectContaining({ factor_name: item.factor_name }))
  expect(host.textContent).toContain('因子预测能力')
  expect(host.textContent).toContain('IC 均值')
  expect(api.customSignalSave).not.toHaveBeenCalled()
})

it('管理员可根据检验方向创建信号，保存后关闭弹窗并刷新信号缓存', async () => {
  await runBatch('admin')
  client.setQueryData(QK.customSignals, { signals: [] })
  expect(signalButton()).not.toBeNull()
  await act(async () => signalButton()!.click())
  expect(host.querySelector('[role="dialog"]')?.textContent).toContain('因子加入信号条件')
  expect(button('保存信号')?.disabled).toBe(false)
  await act(async () => button('保存信号')!.click())
  await settle()
  expect(api.customSignalSave).toHaveBeenCalledWith({
    id: 'f_rsi_14', name: '相对强弱·低值利多', kind: 'entry', enabled: true,
    conditions: [{ left: 'rsi_14', op: '<', right: '30', leftDays: 0, rightDays: 0 }],
  })
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  expect(client.getQueryState(QK.customSignals)?.isInvalidated).toBe(true)
})

it('权限降为普通用户时关闭已有信号创建界面，保留因子筛选结果', async () => {
  await runBatch('admin')
  await act(async () => signalButton()!.click())
  expect(host.querySelector('[role="dialog"]')).not.toBeNull()
  await render('pro')
  expect(signalButton()).toBeNull()
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  expect(host.textContent).toContain('筛选结果')
  expect(host.querySelector(`[aria-label="保存 ${item.label} 为候选"]`)).not.toBeNull()
  expect(api.customSignalSave).not.toHaveBeenCalled()
})
