// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Regime } from './Regime'
import { AuthContext } from '@/lib/auth'
import { QK } from '@/lib/queryKeys'
import type { RegimeRow, User } from '@/lib/api'

const mocks = vi.hoisted(() => ({
  regimeCoverage: vi.fn(), regimeHistory: vi.fn(), regimeStates: vi.fn(),
  regimeObservations: vi.fn(), regimePhases: vi.fn(), regimeMainline: vi.fn(), regimeRecompute: vi.fn(),
}))
vi.mock('@/lib/api', async importOriginal => ({
  ...await importOriginal<object>(), api: mocks,
}))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
vi.mock('echarts', () => ({ init: () => ({
  setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn(),
  getZr: () => ({ on: vi.fn(), off: vi.fn() }),
}) }))

const admin: User = {
  id: 'admin', username: 'admin', role: 'admin', is_initial_admin: true,
  pro_expires_on: null, is_active: true, must_change_password: false,
  created_at: '', last_login_at: null, last_login_ip: null, login_count: 0,
}
const row: RegimeRow = {
  date: '2026-10-16', state: 'range', score: 50, phase: 'repair',
  limit_up: 40, limit_down: 5, broken_limit: 10, max_consecutive: 4,
  seal_rate: 0.8, up_count: 2500, down_count: 2000, up_ratio: 0.55,
  index_pct: 0.01, above_ma20_pct: 0.5, total_amount: 1e12, avg_turnover: 2,
}
let host: HTMLDivElement
let root: Root
let qc: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  mocks.regimeCoverage.mockResolvedValue({ rows: 0, earliest_date: null, latest_date: null })
  mocks.regimeHistory.mockResolvedValue({ rows: [], total: 0 })
  mocks.regimeObservations.mockResolvedValue({ sentiment: null })
  mocks.regimeStates.mockResolvedValue({ distribution: [], days: 0 })
  mocks.regimePhases.mockResolvedValue({ segments: [], total: 0 })
  mocks.regimeMainline.mockResolvedValue({ rows: [], leaders: [] })
  mocks.regimeRecompute.mockResolvedValue({ ok: true, computed: 0 })
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
  for (let i = 0; i < 4; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function render(user: User | null = admin) {
  await act(async () => root.render(
    <AuthContext.Provider value={user}><MemoryRouter><QueryClientProvider client={qc}>
      <Regime />
    </QueryClientProvider></MemoryRouter></AuthContext.Provider>,
  ))
  await settle()
}
function findButton(label: string) {
  return [...host.querySelectorAll('button')].find(button => button.textContent?.trim() === label)
}
function showGeneratedHistory() {
  mocks.regimeCoverage.mockResolvedValue({ rows: 1, earliest_date: row.date, latest_date: row.date })
  mocks.regimeHistory.mockResolvedValue({ rows: [row], total: 1 })
}

it('空态由管理员显式分析，分析中防重复提交，成功后展示结果并失效其他环境缓存', async () => {
  let finish!: (value: { ok: boolean; computed: number }) => void
  mocks.regimeRecompute.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  qc.setQueryData(QK.regimeLatest, { row: null })
  const industryKey = QK.regimeMainline('industry')
  qc.setQueryData(industryKey, { rows: [], leaders: [] })
  await render()
  expect(findButton('环境分析')).toBeDefined()
  expect(mocks.regimeRecompute).not.toHaveBeenCalled()
  await act(async () => findButton('环境分析')!.click())
  await settle()
  expect(findButton('分析中…')?.disabled).toBe(true)
  await act(async () => findButton('分析中…')!.click())
  expect(mocks.regimeRecompute).toHaveBeenCalledExactlyOnceWith()
  showGeneratedHistory()
  await act(async () => finish({ ok: true, computed: 1 }))
  await settle()
  expect(host.textContent).toContain('最新状态 · 2026-10-16')
  expect(host.textContent).not.toContain('后台尚未生成该范围的市场环境数据')
  expect(findButton('环境分析')).toBeUndefined()
  expect(qc.getQueryState(QK.regimeLatest)?.isInvalidated).toBe(true)
  expect(qc.getQueryState(industryKey)?.isInvalidated).toBe(true)
  expect(mocks.regimePhases.mock.calls.length).toBeGreaterThan(1)
  expect(mocks.regimeMainline.mock.calls.length).toBeGreaterThan(1)
})

it('切换短线投机阶段仍能从同一空态发起分析', async () => {
  await render()
  await act(async () => findButton('短线投机阶段')!.click())
  expect(findButton('环境分析')?.closest('.hidden')).toBeNull()
})

it('计算失败展示原因并允许重试', async () => {
  mocks.regimeRecompute.mockRejectedValueOnce(new Error('采集服务不可用'))
  await render()
  await act(async () => findButton('环境分析')!.click())
  await settle()
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('采集服务不可用')
  expect(findButton('环境分析')?.disabled).toBe(false)
  await act(async () => findButton('环境分析')!.click())
  await settle()
  expect(mocks.regimeRecompute).toHaveBeenCalledTimes(2)
})

it('计算为零时提示补齐基础数据，不宣称分析成功', async () => {
  await render()
  await act(async () => findButton('环境分析')!.click())
  await settle()
  expect(host.textContent).toContain('未生成环境数据，请先到数据页同步行情与基础数据')
  expect(host.querySelector('a[href="/data"]')).not.toBeNull()
  expect(findButton('环境分析')?.disabled).toBe(false)
})

it('普通用户看到管理员提示，无共享数据计算入口', async () => {
  await render({ ...admin, role: 'pro' })
  expect(findButton('环境分析')).toBeUndefined()
  expect(host.textContent).toContain('请联系管理员生成市场环境数据')
  expect(mocks.regimeRecompute).not.toHaveBeenCalled()
})

it('加载中和查询失败时不会把结果当作空数据提供分析入口', async () => {
  let reject!: (error: Error) => void
  mocks.regimeHistory.mockImplementationOnce(() => new Promise((_resolve, rejectPromise) => { reject = rejectPromise }))
  await render()
  expect(host.textContent).toContain('加载中…')
  expect(findButton('环境分析')).toBeUndefined()
  await act(async () => reject(new Error('查询失败')))
  await settle()
  expect(host.textContent).toContain('环境数据加载失败')
  expect(findButton('环境分析')).toBeUndefined()
  await act(async () => findButton('重试')!.click())
  await settle()
  expect(findButton('环境分析')).toBeDefined()
})

it('已有环境结果时继续展示数据，刷新只读取结果', async () => {
  showGeneratedHistory()
  await render()
  expect(host.textContent).toContain('最新状态 · 2026-10-16')
  expect(findButton('环境分析')).toBeUndefined()
  await act(async () => findButton('刷新')!.click())
  await settle()
  expect(mocks.regimeRecompute).not.toHaveBeenCalled()
})

it('旧接口没有统一观察契约时不显示旧阶段和参考分', async () => {
  showGeneratedHistory()
  await render()
  expect(host.textContent).toContain('参考分不可计算')
  expect(host.textContent).toContain('状态观察不可计算')
  expect(host.textContent).not.toContain('轻仓试错')
  expect(host.textContent).not.toContain('低位放量')
})

it('切换250/500日范围保留后端固定窗口分位，历史日期可独立选择', async () => {
  const sentiment = { version: 'observations-v1', as_of: row.date, universe: { exclude_st: true, suspension_rule: '剔除停牌' }, coverage: { sample_count: 4500, method_current: true }, context: [], limitations: [], observations: [{ key: 'max_consecutive', label: '最高连板', group: 'speculation', unit: 'count', value: 4, denominator: null, status: 'available', percentile: 73, history_sample_count: 500, history_coverage: 1, change_5d: 1, extreme_streak: 0, extreme_side: null }] }
  mocks.regimeCoverage.mockResolvedValue({ rows: 600, earliest_date: '2024-01-01', latest_date: row.date })
  mocks.regimeHistory.mockResolvedValue({ rows: [{ ...row, method_version: 'observations-v1', method_current: true, sentiment }], total: 1 })
  mocks.regimeObservations.mockResolvedValue({ sentiment })
  await render()
  expect(host.textContent).toContain('P73')
  await act(async () => findButton('2年')!.click())
  await settle()
  expect(host.textContent).toContain('P73')
  expect(host.querySelector('select#observation-date')).not.toBeNull()
})

it('瘦身历史不依赖每行sentiment；切日期隔离旧值并处理加载、失败、重试和空数据', async () => {
  const previous = { ...row, date: '2026-10-15', method_version: 'observations-v1', method_current: true }
  mocks.regimeHistory.mockResolvedValue({ rows: [previous, { ...row, method_version: 'observations-v1', method_current: true }], total: 2 })
  mocks.regimeObservations.mockResolvedValue({ sentiment: { version: 'observations-v1', as_of: row.date, universe: { exclude_st: true, suspension_rule: '剔除停牌' }, coverage: { sample_count: 4500, method_current: true }, context: [], limitations: [], observations: [{ key: 'max_consecutive', label: '最高连板', group: 'speculation', unit: 'count', value: 4, denominator: null, status: 'available', percentile: 73, history_sample_count: 500, history_coverage: 1, change_5d: 1, extreme_streak: 0, extreme_side: null }] } })
  await render()
  expect(host.textContent).toContain('P73')
  expect(host.textContent).not.toContain('参考分不可计算')
  let reject!: (error: Error) => void
  mocks.regimeObservations.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail }))
  const select = host.querySelector<HTMLSelectElement>('#observation-date')!
  await act(async () => { select.value = previous.date; select.dispatchEvent(new Event('change', { bubbles: true })) })
  await settle()
  expect(mocks.regimeObservations).toHaveBeenLastCalledWith(previous.date)
  expect(host.textContent).toContain('状态观察加载中')
  expect(host.textContent).not.toContain('P73')
  await act(async () => reject(new Error('network')))
  await settle()
  expect(host.textContent).toContain('状态观察加载失败')
  mocks.regimeObservations.mockResolvedValue({ sentiment: null })
  await act(async () => findButton('重试观察')!.click())
  await settle()
  expect(host.textContent).toContain('状态观察不可计算')
  expect(mocks.regimeObservations).toHaveBeenCalledTimes(3)
})

it('方法版本相同但ST配置不匹配时不展示旧评分和阶段', async () => {
  mocks.regimeHistory.mockResolvedValue({ rows: [{ ...row, method_version: 'observations-v1', method_current: false }], total: 1 })
  await render()
  expect(host.textContent).toContain('参考分不可计算')
  await act(async () => findButton('短线投机阶段')!.click())
  expect(host.textContent).not.toContain('较5个交易日前，二板宽度和晋级率同时改善')
  expect(host.textContent).toContain('不可计算')
})

it('只有方法版本但缺少配置校验标记的旧schema不默认为当前', async () => {
  mocks.regimeHistory.mockResolvedValue({ rows: [{ ...row, method_version: 'observations-v1' }], total: 1 })
  await render()
  expect(host.textContent).toContain('参考分不可计算')
})
