// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StockPanel } from './StockPanel'

const mocks = vi.hoisted(() => ({ daily: vi.fn(), boxes: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: {
  klineDaily: mocks.daily, klineBoxes: mocks.boxes,
  klineGaps: async (symbol: string) => ({ symbol, status: 'ready', gaps: [] }),
} }))
vi.mock('@/lib/useSharedQueries', () => ({ useCapabilities: () => ({ data: {} }) }))
vi.mock('@/lib/useFinancials', () => ({ useFinancialMetrics: () => ({ data: undefined }), financialMetricsQueryOptions: vi.fn() }))
vi.mock('@/components/StockInfoBar', () => ({ StockInfoBar: () => null }))
vi.mock('@/components/StockIntradayChart', () => ({ StockIntradayChart: ({ date, prevClose, dailySummary }: any) =>
  <div data-intraday data-date={date} data-base={prevClose} data-close={dailySummary?.close} />,
}))
vi.mock('@/components/EChartsCandlestick', async importOriginal => ({
  ...await importOriginal<typeof import('./EChartsCandlestick')>(),
  EChartsCandlestick: ({ data, onDateClick }: any) => <div>{data.map((row: any) =>
    <button key={row.date} data-day={row.date} onClick={() => onDateClick(row.date)}>{row.date}</button>)}</div>,
}))

let host: HTMLDivElement
let root: ReturnType<typeof createRoot>
let client: QueryClient
const range = { start: '2026-04-01', end: '2026-10-26' }
const row = (date: string, close: number) => ({ date, open: close, high: close + 1, low: close - 1, close, volume: 1 })
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.daily.mockReset().mockImplementation(async (symbol: string) => ({ symbol, rows: [row('2026-10-22', 20), row('2026-10-23', 22), row('2026-10-26', 23)] }))
  mocks.boxes.mockReset().mockImplementation(async (symbol: string, period: string) => ({ symbol, period, status: 'ready', boxes: [], diagnostics: [],
    rows: [row('2023-01-06', 10)] }))
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
async function settle() {
  for (let i = 0; i < 5; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function click(label: string) {
  const button = [...host.querySelectorAll<HTMLButtonElement>('button')].find(item => item.textContent === label)!
  await act(async () => button.click()); await settle()
}

it('周K点击旧日期时按需取该日实际前收，加载期间不套用当前前收且默认不额外请求', async () => {
  await act(async () => root.render(<QueryClientProvider client={client}><StockPanel symbol="600000.SH" dateRange={range} /></QueryClientProvider>))
  await settle()
  expect(mocks.daily).toHaveBeenCalledTimes(1)
  expect(host.querySelector('[data-intraday]')?.getAttribute('data-base')).toBe('22')
  let resolveHistory: (value: unknown) => void = () => {}
  mocks.daily.mockImplementation((symbol: string, _days: number, requested: { start: string; end: string }) => requested.end === '2023-01-06'
    ? new Promise(resolve => { resolveHistory = resolve })
    : Promise.resolve({ symbol, rows: [row('2026-10-22', 20), row('2026-10-23', 22), row('2026-10-26', 23)] }))
  await click('周K')
  await click('2023-01-06')
  expect(host.querySelector('[data-intraday]')?.getAttribute('data-date')).toBe('2023-01-06')
  expect(host.querySelector('[data-intraday]')?.getAttribute('data-base')).toBeNull()
  expect(mocks.daily).toHaveBeenCalledWith('600000.SH', undefined, { start: '2022-01-06', end: '2023-01-06' }, undefined)
  await act(async () => resolveHistory({ symbol: '600000.SH', rows: [row('2023-01-05', 8), row('2023-01-06', 10)] }))
  await settle()
  expect(host.querySelector('[data-intraday]')?.getAttribute('data-base')).toBe('8')
  expect(host.querySelector('[data-intraday]')?.getAttribute('data-close')).toBe('10')
})
