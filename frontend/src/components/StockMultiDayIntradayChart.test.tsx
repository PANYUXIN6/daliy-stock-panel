// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StockMultiDayIntradayChart } from './StockMultiDayIntradayChart'

const mocks = vi.hoisted(() => ({ sync: vi.fn(), range: vi.fn(), latest: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: {
  syncMinuteSingle: mocks.sync,
  klineMinuteRange: mocks.range,
  klineMinute: mocks.latest,
} }))
vi.mock('@/components/EChartsMultiDayIntraday', () => ({ EChartsMultiDayIntraday: ({ sessions }: { sessions: { date: string; rows: { close: number }[] }[] }) => <div data-chart>{sessions.map(session => `${session.date}:${session.rows[0].close}`).join('|')}</div> }))
let root: Root
let host: HTMLDivElement
let client: QueryClient
async function settle() {
  for (let i = 0; i < 5; i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  mocks.range.mockResolvedValue({ sessions: [], asset_type: 'stock' })
  mocks.latest.mockResolvedValue({ date: '2026-10-09', rows: [], asset_type: 'stock' })
  host = document.createElement('div'); document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
async function render(symbol = '600000.SH') {
  await act(async () => root.render(<QueryClientProvider client={client}><StockMultiDayIntradayChart symbol={symbol} days={5} /></QueryClientProvider>))
  await settle()
}
async function click(text: string) {
  const button = [...host.querySelectorAll('button')].find(b => b.textContent?.includes(text))
  expect(button).toBeDefined()
  await act(async () => button!.click()); await settle()
}
it('shows the saved coverage and refreshes local data without collecting missing sessions', async () => {
  const session = { date: '2026-10-08', rows: [{ datetime: '2026-10-08T09:35:00', close: 10 }] }
  mocks.range.mockResolvedValue({ sessions: [session], asset_type: 'stock' })
  await render()
  expect(host.textContent).toContain('已采集 1 / 5 个交易日')
  expect(host.querySelector('[data-chart]')?.textContent).toBe('2026-10-08:10')
  mocks.latest.mockResolvedValue({ date: '2026-10-09', rows: [{ datetime: '2026-10-09T09:35:00', close: 12 }], asset_type: 'stock' })
  await click('刷新')
  expect(host.querySelector('[data-chart]')?.textContent).toBe('2026-10-08:10|2026-10-09:12')
  expect(mocks.sync).not.toHaveBeenCalled()
})
it('shows a local empty state after changing stocks without collecting data', async () => {
  await render(); await render('000001.SZ')
  expect(host.textContent).toContain('后台尚未采集可展示的分钟数据')
  expect(mocks.sync).not.toHaveBeenCalled()
})
it('retries failed local reads without starting collection', async () => {
  mocks.range.mockRejectedValueOnce(new Error('本地数据读取失败'))
  await render()
  expect(host.textContent).toContain('本地数据读取失败')
  await click('重新加载')
  expect(host.textContent).toContain('后台尚未采集可展示的分钟数据')
  expect(mocks.sync).not.toHaveBeenCalled()
})
