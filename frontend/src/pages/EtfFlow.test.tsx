// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { EtfFlowBoard, EtfReference } from '@/lib/api'
import { EtfFlow } from './EtfFlow'

const fixtures = vi.hoisted(() => ({
  load: vi.fn(), reference: vi.fn(), sync: vi.fn(), admin: false,
}))
vi.mock('@/lib/api', () => ({ api: {
  etfFlow: fixtures.load, etfReference: fixtures.reference, syncEtfFlow: fixtures.sync,
  pipelineJobs: async () => ({ jobs: [] }),
  pipelineJob: async () => ({ status: 'running', log: [{ msg: '正在采集份额' }] }),
  pipelineJobCancel: async () => ({}),
} }))
vi.mock('@/lib/auth', () => ({ useCurrentUser: () => ({ role: fixtures.admin ? 'admin' : 'guest' }) }))
vi.mock('@/lib/useSharedQueries', () => ({ useCapabilityMatrix: () => ({ data: { capabilities: [] } }) }))
vi.mock('./etfFlow/FlowChart', () => ({ FlowChart: ({ title, series, labels }: { title: string; series: unknown; labels: string[] }) =>
  <section aria-label={title} data-labels={JSON.stringify(labels)}><h3>{title}</h3><pre>{JSON.stringify(series)}</pre></section>,
}))
vi.mock('./etfFlow/EtfReferenceChart', () => ({ default: ({ data }: { data?: EtfReference }) =>
  <section aria-label="国家队持仓占比参考">国家队持仓占比参考 {data?.as_of}</section>,
}))

function response(day = '2026-11-05'): EtfFlowBoard {
  const missing = { net_flow_yuan: null, available_flow_yuan: 10, covered: 1, expected: 2 }
  const complete = { net_flow_yuan: 1e8, available_flow_yuan: 1e8, covered: 2, expected: 2 }
  return {
    state: 'ready', selected_date: day, dates: ['2026-11-04', '2026-11-05'],
    calendar_through: '2026-11-05', calendar_current: true,
    updated_at: null, status: {}, sources: {}, catalog_as_of: null, funds: [], categories: ['沪深300'],
    daily: [
      { ...complete, broad: complete, date: '2026-11-03', amount_yuan: 2e8, amount_covered: 2, index_close: 4000, signal: false, categories: { 沪深300: complete } },
      { ...missing, broad: missing, date: '2026-11-04', amount_yuan: null, amount_covered: 1, index_close: null, signal: null, categories: { 沪深300: missing } },
    ],
    coverage: { start: '2026-11-03', end: '2026-11-04', daily_days: 2, flow_days: 1,
      minute_days: 0, minute_start: null, minute_end: null, comparison_days: 20 },
    history: { daily: { target_start: '2024-01-01', start: '2024-01-02', end: '2026-11-04', expected_days: 692,
      flow_days: 691, amount_days: 692, flow_start: '2024-01-02', flow_end: '2026-11-03', broad_flow_days: 680, missing_flow_dates: ['2026-11-04'] },
      minute: { start: '2025-01-27', end: '2026-11-05', expected_days: 430, observed_days: 430, complete_days: 429,
        complete_start: '2025-01-27', missing_dates: ['2026-11-05'], symbols: [] } },
    intraday: { date: day, as_of: null, amount_yuan: null, covered: 0, expected: 20, complete: false,
      points: [{ time: '09:30', amount_yuan: null, cumulative_yuan: null, covered: 0, index_close: null }],
      ranking: [{ symbol: '510300.SH', name: '沪深300ETF华泰柏瑞', category: '沪深300', amount_yuan: null,
        change_pct: null, weight: null, minutes: 0, expected_minutes: 1 }] },
    previous: null, average: [], signal_average: [], normal_average: [], cases: [], threshold_yuan: 1e10,
  }
}

let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  fixtures.admin = false
  fixtures.load.mockReset().mockImplementation(async (day?: string) => response(day))
  fixtures.reference.mockReset().mockImplementation(async (day?: string) => ({ state: 'no_data', rows: [], as_of: day ?? null,
    checked_at: null, update_failed: false }))
  fixtures.sync.mockReset().mockResolvedValue({ job_id: 'flow-job' })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity, staleTime: Infinity } } })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})
async function settle() {
  for (let i = 0; i < 5; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function mount() {
  await act(async () => root.render(<MemoryRouter><QueryClientProvider client={client}><EtfFlow /></QueryClientProvider></MemoryRouter>))
  await settle()
}

it('preserves gaps and refreshes the reference chart at the bottom without exposing collection to viewers', async () => {
  await mount()
  expect(host.textContent).toContain('重点ETF资金看板')
  const ranking = host.querySelector('[aria-label="单只ETF表现"]')!
  expect(ranking.textContent).toContain('—')
  expect(ranking.textContent).not.toContain('0.00')
  expect(ranking.textContent).not.toContain('按成交额降序')
  const help = ranking.querySelector<HTMLButtonElement>('button[aria-label="单只ETF表现口径"]')!
  await act(async () => help.click())
  expect(document.getElementById(help.getAttribute('aria-describedby')!)?.textContent).toContain('按成交额降序')
  const chart = host.querySelector('[aria-label="宽基ETF日度净流入与累计净流入"] pre')!
  expect(JSON.parse(chart.textContent!).at(-1)).toMatchObject({ name: '累计净流入', data: [1, null], rightAxis: true })
  expect(host.textContent).not.toContain('同步数据')
  expect(host.querySelector('main')?.lastElementChild?.getAttribute('aria-label')).toBe('国家队持仓占比参考')
  const checks = fixtures.reference.mock.calls.length
  await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent === '刷新')!.click())
  await settle()
  expect(fixtures.reference.mock.calls.length).toBe(checks + 1)
})

it('keeps cumulative excess gaps and highlights the last three sessions even when they are signal days', async () => {
  const board = response()
  board.signal_average = [3e8, null, 4e8].map((amount, i) => ({ time: `09:3${i}`, amount_yuan: amount,
    cumulative_yuan: null, samples: 1, cumulative_samples: 0 }))
  board.normal_average = [1e8, 1e8, 1e8].map((amount, i) => ({ time: `09:3${i}`, amount_yuan: amount,
    cumulative_yuan: null, samples: 1, cumulative_samples: 0 }))
  board.daily = ['2026-10-29', '2026-10-30', '2026-11-02', '2026-11-03', '2026-11-04'].map((date, i) => ({
    ...board.daily[0], date, signal: i === 0 || i === 3, amount_yuan: (i + 1) * 1e8,
  }))
  fixtures.load.mockResolvedValueOnce(board)
  await mount()
  const profile = JSON.parse(host.querySelector('[aria-label="放量案例日 · 重点ETF分钟成交额"] pre')!.textContent!)
  expect(profile[1]).toMatchObject({ name: '较常态日多成交', data: [2, null, null], rightAxis: true })
  const amountChart = host.querySelector('[aria-label^="日度成交额："]')!
  expect(JSON.parse(amountChart.getAttribute('data-labels')!)).toEqual(['2026-10-29', '2026-11-02', '2026-11-03', '2026-11-04'])
  const bars = JSON.parse(amountChart.querySelector('pre')!.textContent!)[0]
  expect(bars.data).toEqual([1, 3, 4, 5])
  expect(bars.pointColors).toEqual([null, '#d7a658', '#d7a658', '#d7a658'])
})

it('does not display the previous date while a newly selected date is loading', async () => {
  await mount()
  let resolve: (board: EtfFlowBoard) => void = () => undefined
  fixtures.load.mockImplementationOnce(() => new Promise<EtfFlowBoard>(done => { resolve = done }))
  const select = host.querySelector<HTMLSelectElement>('[aria-label="观察日期"]')!
  await act(async () => {
    select.value = '2026-11-04'
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })
  expect(host.textContent).toContain('加载ETF资金数据')
  expect(host.querySelector('[aria-label="单只ETF表现"]')).toBeNull()
  await act(async () => resolve(response('2026-11-04')))
  await settle()
  expect(host.querySelector('#etf-minute')?.textContent).toContain('2026-11-04')
  expect(fixtures.reference).toHaveBeenLastCalledWith('2026-11-04')
  expect(host.querySelector('[aria-label="国家队持仓占比参考"]')?.textContent).toContain('2026-11-04')
})

it('starts the selected collection window and follows the shared job progress', async () => {
  fixtures.admin = true
  await mount()
  const window = host.querySelector<HTMLSelectElement>('[aria-label="历史窗口"]')!
  await act(async () => {
    window.value = '60'
    window.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await settle()
  const button = [...host.querySelectorAll('button')].find(b => b.textContent === '同步数据')!
  await act(async () => button.click())
  await settle()
  expect(fixtures.sync).toHaveBeenCalledWith(60, false)
  expect(host.textContent).toContain('正在采集份额')
  expect(host.textContent).toContain('取消同步')
  expect([...host.querySelectorAll('button')].find(b => b.textContent === '同步中…')?.disabled).toBe(true)
})

it('shows a retryable loading error instead of zero-filled data', async () => {
  fixtures.load.mockRejectedValueOnce(new Error('读取失败样本'))
  await mount()
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('读取失败样本')
  expect(host.querySelector('[aria-label="单只ETF表现"]')).toBeNull()
  const retry = [...host.querySelectorAll('button')].find(b => b.textContent === '重试')!
  await act(async () => retry.click())
  await settle()
  expect(host.querySelector('[aria-label="单只ETF表现"]')).not.toBeNull()
})


it('supports all-history viewing and requests resumable full history', async () => {
  fixtures.admin = true
  await mount()
  const window = host.querySelector<HTMLSelectElement>('[aria-label="历史窗口"]')!
  await act(async () => {
    window.value = '0'
    window.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await settle()
  expect(fixtures.load).toHaveBeenLastCalledWith(undefined, 0)
  const button = [...host.querySelectorAll('button')].find(b => b.textContent === '补采历史')!
  await act(async () => button.click())
  await settle()
  expect(fixtures.sync).toHaveBeenCalledWith(22, true)
})
