// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { StockFinancialDetail } from './StockFinancialDetail'

const mocks = vi.hoisted(() => ({ read: vi.fn(), fetch: vi.fn(), options: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: {
  financialMetrics: mocks.read, financialIncome: mocks.read, financialBalanceSheet: mocks.read,
  financialCashFlow: mocks.read, financialShares: mocks.read, financialFetch: mocks.fetch,
  financialStatus: async () => ({ tables: {} }), financialCollectionOptions: mocks.options,
} }))
vi.mock('@/lib/aiReportStore', () => ({ startAnalysis: vi.fn(), findLatestHistoryReport: vi.fn(), openHistoryReport: vi.fn() }))
vi.mock('@/lib/useAiCredits', () => ({ useAiFeature: () => ({ available: false, priceLabel: '5 点', reason: '额度未加载' }) }))

const host = document.createElement('div')
const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
let root = createRoot(host)
async function settle() {
  for (let i = 0; i < 5; i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}
async function render(symbol = '600000.SH') {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  await act(async () => root.render(<QueryClientProvider client={qc}><StockFinancialDetail symbol={symbol} name={symbol} /></QueryClientProvider>))
  await settle()
}
afterEach(async () => {
  await act(async () => root.unmount())
  root = createRoot(host)
  qc.clear()
  vi.resetAllMocks()
})

it('单击只获取当前股票当前表，等待时禁用，成功后重新读取落库结果', async () => {
  let resolve!: (result: unknown) => void
  mocks.options.mockResolvedValue({ tables: ['metrics', 'income'] })
  mocks.read.mockResolvedValue({ data: [] })
  mocks.fetch.mockImplementation(() => new Promise(r => { resolve = r }))
  await render()
  const get = [...host.querySelectorAll('button')].find(b => b.textContent === '获取该股核心指标')!
  await act(async () => get.click()); await settle()
  expect(mocks.fetch).toHaveBeenCalledExactlyOnceWith('600000.SH', 'metrics')
  expect(get.disabled).toBe(true)
  expect(host.textContent).toContain('正在获取并保存')
  mocks.read.mockResolvedValue({ data: [{ symbol: '600000.SH', period_end: '2026-06-30', roe: 12.3 }] })
  await act(async () => resolve({ symbol: '600000.SH', table: 'metrics', rows: 1, reused: false }))
  await settle()
  expect(host.textContent).toContain('12.30%')
})

it('失败可重试；切股后前一只股票的失败和加载状态不串入当前股票', async () => {
  mocks.options.mockResolvedValue({ tables: ['metrics'] })
  mocks.read.mockResolvedValue({ data: [] })
  mocks.fetch.mockRejectedValue(new Error('数据源暂不可用'))
  await render()
  const get = () => [...host.querySelectorAll('button')].find(b => b.textContent === '获取该股核心指标')!
  await act(async () => get().click()); await settle()
  expect(host.querySelector('[role="alert"]')?.textContent).toBe('数据源暂不可用')
  expect(get().disabled).toBe(false)
  await render('000001.SZ')
  expect(host.textContent).not.toContain('数据源暂不可用')
  expect(get().disabled).toBe(false)
  await act(async () => get().click()); await settle()
  expect(mocks.fetch).toHaveBeenLastCalledWith('000001.SZ', 'metrics')
})

it('不支持的表显示明确提示；切换标签不自动采集', async () => {
  mocks.options.mockResolvedValue({ tables: ['income'] })
  mocks.read.mockResolvedValue({ data: [] })
  await render()
  expect(host.textContent).toContain('当前数据源不支持此项获取')
  await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent === '利润表')!.click())
  expect(host.textContent).toContain('获取该股利润表')
  expect(mocks.fetch).not.toHaveBeenCalled()
})

it('采集更多使用当前表游标，等待时防重复点击，每次成功后再向前一年', async () => {
  let resolve!: (result: unknown) => void
  mocks.options.mockResolvedValue({ tables: ['metrics', 'income'] })
  mocks.read.mockResolvedValue({ data: [{ period_end: '2026-06-30', roe: 12.3 }], history_start: '2024-10-18' })
  mocks.fetch.mockImplementation(() => new Promise(r => { resolve = r }))
  await render()
  const more = () => [...host.querySelectorAll('button')].find(b => b.textContent === '采集更多（前 1 年）')!
  expect(host.textContent).toContain('已检查至 2024-10-18')
  const button = more()
  await act(async () => button.click()); await settle()
  await act(async () => button.click()); await settle()
  expect(mocks.fetch).toHaveBeenCalledExactlyOnceWith('600000.SH', 'metrics', '2024-10-18')
  expect(button.disabled).toBe(true)
  expect(host.textContent).toContain('正在采集前 1 年')
  mocks.read.mockResolvedValue({ data: [{ period_end: '2026-06-30', roe: 12.3 }], history_start: '2023-10-18' })
  await act(async () => resolve({ rows: 0, reused: false })); await settle()
  expect(host.textContent).toContain('该年度未返回财务数据')
  expect(host.textContent).toContain('已检查至 2023-10-18')
  await act(async () => more().click()); await settle()
  expect(mocks.fetch).toHaveBeenLastCalledWith('600000.SH', 'metrics', '2023-10-18')
  await act(async () => resolve({ rows: 1, reused: false })); await settle()
  await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent === '利润表')!.click())
  await act(async () => more().click()); await settle()
  expect(mocks.fetch).toHaveBeenLastCalledWith('600000.SH', 'income', '2024-10-18')
  await act(async () => resolve({ rows: 1, reused: false })); await settle()
})

it('补采失败保留数据与游标，重试仍获取同一年', async () => {
  mocks.options.mockResolvedValue({ tables: ['metrics'] })
  mocks.read.mockResolvedValue({ data: [{ period_end: '2026-06-30', roe: 12.3 }], history_start: '2024-10-18' })
  mocks.fetch.mockRejectedValue(new Error('数据源限流'))
  await render()
  const more = () => [...host.querySelectorAll('button')].find(b => b.textContent === '采集更多（前 1 年）')!
  await act(async () => more().click()); await settle()
  expect(host.textContent).toContain('12.30%')
  expect(host.textContent).toContain('数据源限流')
  expect(host.textContent).toContain('已检查至 2024-10-18')
  await act(async () => more().click()); await settle()
  expect(mocks.fetch).toHaveBeenLastCalledWith('600000.SH', 'metrics', '2024-10-18')
})

it('部分成功展示已保存指标与持久化缺期提示，重试补齐后清除提示', async () => {
  mocks.options.mockResolvedValue({ tables: ['metrics'] })
  mocks.read.mockResolvedValue({ data: [] })
  mocks.fetch.mockImplementation(async () => {
    mocks.read.mockResolvedValue({
      data: [{ period_end: '2026-06-30', roe: 12.3 }], history_start: '2024-10-18',
      missing_periods: ['2026-09-30'],
    })
    return { rows: 1, complete: false, missing_periods: ['2026-09-30'] }
  })
  await render()
  await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent === '获取该股核心指标')!.click())
  await settle()
  expect(host.textContent).toContain('12.30%')
  expect(host.textContent).toContain('数据源暂未提供以下报告期：2026-09-30')
  expect(host.querySelector('[role="alert"]')).toBeNull()
  mocks.fetch.mockImplementation(async () => {
    mocks.read.mockResolvedValue({ data: [{ period_end: '2026-09-30', roe: 14.5 }], missing_periods: [] })
    return { rows: 1, complete: true, missing_periods: [] }
  })
  await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent === '重试补齐')!.click())
  await settle()
  expect(mocks.fetch).toHaveBeenLastCalledWith('600000.SH', 'metrics')
  expect(host.textContent).toContain('14.50%')
  expect(host.textContent).not.toContain('暂未提供')
})

it('重载可见更早年度的缺期，重试仍取原年度，切股或切表不串提示', async () => {
  mocks.options.mockResolvedValue({ tables: ['metrics', 'income'] })
  mocks.read.mockResolvedValue({
    data: [{ period_end: '2026-06-30', roe: 12.3 }], history_start: '2024-10-18',
    missing_periods: ['2024-03-31'],
  })
  mocks.fetch.mockResolvedValue({ rows: 0, complete: false, missing_periods: ['2024-03-31'] })
  await render()
  expect(host.textContent).toContain('暂未提供以下报告期：2024-03-31')
  await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent === '重试补齐')!.click())
  await settle()
  expect(mocks.fetch).toHaveBeenLastCalledWith('600000.SH', 'metrics', '2024-10-18')
  expect(host.textContent).not.toContain('该年度未返回财务数据')
  mocks.read.mockResolvedValue({ data: [] })
  await render('000001.SZ')
  expect(host.textContent).not.toContain('2024-03-31')
  await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent === '利润表')!.click())
  await settle()
  expect(host.textContent).not.toContain('2024-03-31')
})
