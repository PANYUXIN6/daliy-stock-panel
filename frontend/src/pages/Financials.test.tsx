// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, it, vi } from 'vitest'
import { Financials } from './Financials'

const mocks = vi.hoisted(() => ({ status: vi.fn(), execution: vi.fn(), sync: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: {
  financialStatus: mocks.status,
  financialExecutionStatus: mocks.execution,
  financialSync: mocks.sync,
} }))
vi.mock('@/components/financials/StockFinancialSearch', () => ({ StockFinancialSearch: ({ onSelect }: { onSelect: (symbol: string, name: string) => void }) => <button onClick={() => onSelect('600000.SH', '浦发银行')}>选择浦发银行</button> }))
vi.mock('@/components/financials/StockFinancialDetail', () => ({ StockFinancialDetail: ({ symbol }: { symbol: string }) => <div>{symbol} 财务详情与 AI 分析</div> }))
vi.mock('@/components/financials/ReportHistoryPanel', () => ({ ReportHistoryPanel: () => <div>历史分析报告</div> }))
vi.mock('@/lib/useLastStock', () => ({ useLastStock: () => ({ last: null, remember: vi.fn() }) }))

async function settle() {
  for (let i = 0; i < 5; i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

it('财务库全空时仍可搜索并进入单股获取', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.status.mockResolvedValue({ available: false, tables: {} })
  mocks.execution.mockResolvedValue({ last_sync: {}, syncing: false })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const host = document.createElement('div'); const root = createRoot(host)
  try {
    await act(async () => root.render(<MemoryRouter><QueryClientProvider client={qc}><Financials /></QueryClientProvider></MemoryRouter>))
    await settle()
    await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent === '选择浦发银行')!.click())
    expect(host.textContent).toContain('600000.SH 财务详情与 AI 分析')
    expect(mocks.sync).not.toHaveBeenCalled()
  } finally { await act(async () => root.unmount()); qc.clear() }
})

it('browses saved financial tables without provider capability and refreshes only local status', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.execution.mockReset().mockResolvedValue({ last_sync: {}, syncing: false, active_job_id: null })
  mocks.sync.mockReset()
  mocks.status.mockReset().mockResolvedValue({ available: false, tables: { metrics: { rows: 12, symbols: 2 } }, last_sync: {}, syncing: false })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const host = document.createElement('div'); const root = createRoot(host)
  try {
    await act(async () => root.render(<MemoryRouter><QueryClientProvider client={qc}><Financials /></QueryClientProvider></MemoryRouter>))
    await settle()
    expect(host.textContent).toContain('12')
    expect(host.textContent).toContain('历史分析报告')
    const pick = [...host.querySelectorAll('button')].find(b => b.textContent === '选择浦发银行')!
    await act(async () => pick.click())
    expect(host.textContent).toContain('600000.SH 财务详情与 AI 分析')
    mocks.status.mockResolvedValue({ available: false, tables: { metrics: { rows: 20, symbols: 3 } }, last_sync: {}, syncing: false })
    const refresh = [...host.querySelectorAll('button')].find(b => b.textContent === '刷新')!
    await act(async () => refresh.click()); await settle()
    expect(host.textContent).toContain('20')
    expect(mocks.sync).not.toHaveBeenCalled()
  } finally { await act(async () => root.unmount()); qc.clear() }
})


it('采集服务离线时新浏览器仍可访问本地画像、搜索、报表和历史报告', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.status.mockReset().mockResolvedValue({ available: true, tables: { metrics: { rows: 75, symbols: 3 } } })
  mocks.execution.mockReset().mockRejectedValue(new Error('采集服务未连接'))
  mocks.sync.mockReset()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const host = document.createElement('div'); const root = createRoot(host)
  try {
    await act(async () => root.render(<MemoryRouter><QueryClientProvider client={qc}><Financials /></QueryClientProvider></MemoryRouter>))
    await settle()
    expect(host.textContent).toContain('75')
    expect(host.textContent).toContain('已保存，按需更新')
    expect(host.textContent).toContain('历史分析报告')
    const pick = [...host.querySelectorAll('button')].find(node => node.textContent === '选择浦发银行')!
    await act(async () => pick.click())
    expect(host.textContent).toContain('600000.SH 财务详情与 AI 分析')
    expect(mocks.sync).not.toHaveBeenCalled()
  } finally { await act(async () => root.unmount()); qc.clear() }
})
