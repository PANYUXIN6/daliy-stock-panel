// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QK } from '@/lib/queryKeys'
import { StockDailyKChart } from './StockDailyKChart'

const mocks = vi.hoisted(() => ({ daily: vi.fn(), earnings: vi.fn(), gaps: vi.fn(), boxes: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { klineDaily: mocks.daily, klineEarningsBreakout: mocks.earnings, klineGaps: mocks.gaps, klineBoxes: mocks.boxes } }))
vi.mock('@/components/EChartsCandlestick', async importOriginal => ({
  ...await importOriginal<typeof import('./EChartsCandlestick')>(),
  EChartsCandlestick: ({ data, activeIndicators, markers, showMarkers, priceGaps, boxes, ranges, priceLines }: any) => <div data-chart data-rows={JSON.stringify(data)} data-ranges={ranges?.length ?? 0} data-lines={priceLines?.length ?? 0}>
    {activeIndicators.includes('bull_bear_line') ? `多空线:${data.at(-1)?.bull_bear_line ?? '—'}` : '指标关闭'}
    <span data-markers>{showMarkers ? markers.map((m: any) => `${m.date}:${m.label}`).join(',') : ''}</span>
    <span data-gaps>{priceGaps?.length ? JSON.stringify(priceGaps) : ''}</span>
    <span data-boxes>{boxes?.map((box: any) => box.id).join(',')}</span>
  </div>,
}))

let host: HTMLDivElement
let root: ReturnType<typeof createRoot>
let client: QueryClient
const range = { start: '2026-01-01', end: '2026-01-31' }
const response = (symbol: string, value: number | null) => ({ symbol, rows: [{
  date: '2026-01-05', open: 10, high: 12, low: 9, close: 11, bull_bear_line: value,
}] })

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.daily.mockReset()
  mocks.earnings.mockReset()
  mocks.gaps.mockReset()
  mocks.boxes.mockReset().mockImplementation(async (symbol: string, period: string) => ({
    symbol, period, status: 'ready', history_start: range.start, as_of: range.end, rows: [], boxes: [], diagnostics: [],
  }))
  mocks.gaps.mockImplementation(async (symbol: string) => ({
    symbol, status: 'ready', history_start: range.start, as_of: range.end, gaps: [],
  }))
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
async function settle() {
  for (let i = 0; i < 5; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function render(symbol = '600000.SH') {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <StockDailyKChart symbol={symbol} dateRange={range} />
  </QueryClientProvider>))
  await settle()
}
async function toggle() {
  const button = [...host.querySelectorAll('button')].find(button => button.textContent === '多空线')
  expect(button).toBeDefined()
  await act(async () => button!.click())
}

it('多空线默认选中且可关闭，切股和实时更新使用当前数据，旧响应允许缺字段', async () => {
  mocks.daily.mockResolvedValue(response('600000.SH', 10.25))
  await render()
  expect(host.querySelector('[data-chart]')?.textContent).toBe('多空线:10.25')
  mocks.daily.mockResolvedValue(response('000001.SZ', 20.5))
  await render('000001.SZ')
  expect(host.querySelector('[data-chart]')?.textContent).toBe('多空线:20.5')
  await act(async () => client.setQueryData(QK.kline('000001.SZ', range.start, range.end), response('000001.SZ', 21)))
  await settle()
  expect(host.querySelector('[data-chart]')?.textContent).toBe('多空线:21')
  mocks.daily.mockResolvedValue({ symbol: '600002.SH', rows: [{ date: '2026-01-05', open: 10, high: 12, low: 9, close: 11 }] })
  await render('600002.SH')
  expect(host.querySelector('[data-chart]')?.textContent).toBe('多空线:—')
  await toggle()
  expect(host.querySelector('[data-chart]')?.textContent).toBe('指标关闭')
})

it('加载、空数据和错误状态没有可操作的指标按钮', async () => {
  mocks.daily.mockReturnValue(new Promise(() => {}))
  await render()
  expect(host.textContent).toContain('加载中')
  expect(host.querySelector('[data-chart]')).toBeNull()
  mocks.daily.mockResolvedValue({ symbol: '000001.SZ', rows: [] })
  await render('000001.SZ')
  expect([...host.querySelectorAll('button')].some(button => button.textContent === '多空线')).toBe(false)
  mocks.daily.mockRejectedValue(new Error('读取失败'))
  await render('600002.SH')
  expect(host.textContent).toContain('日K加载失败')
  expect(host.querySelector('[data-chart]')).toBeNull()
})

async function clickPill(label: string) {
  const button = [...host.querySelectorAll('button')].find(button => button.textContent === label)
  expect(button).toBeDefined()
  await act(async () => button!.click())
  await settle()
}

it('业绩断层按需读取、异动开关独立，切股与缺依据不会泄漏旧标记', async () => {
  mocks.daily.mockResolvedValue(response('600000.SH', 10.25))
  mocks.earnings.mockResolvedValue({ symbol: '600000.SH', source: 'fuyao', status: 'ready', reports: 2, eligible_reports: 1,
    events: [{ date: '2026-01-05', period_end: '2025-12-31' }],
    markers: [{ date: '2026-01-05', icon: 9, event_date: '2026-01-02' }, { date: '2026-01-05', icon: 42, event_date: '2026-01-02' }] })
  await render()
  expect(mocks.earnings).not.toHaveBeenCalled()
  await clickPill('业绩断层')
  expect(host.querySelector('[data-markers]')?.textContent).toBe('2026-01-05:断,2026-01-05:缺口,2026-01-05:中点')
  const gapLegend = [...host.querySelectorAll('span')].find(span => span.textContent === '↑ 缺口回踩')
  const midpointLegend = [...host.querySelectorAll('span')].find(span => span.textContent === '◆ 中点回踩')
  expect(gapLegend?.title).toContain('PSY(12)≤50')
  expect(midpointLegend?.title).toContain('当天最高价与最低价的平均值')
  await clickPill('异动')
  expect(host.querySelector('[data-markers]')?.textContent).toContain(':中点')
  mocks.daily.mockResolvedValue(response('000001.SZ', 20.5))
  mocks.earnings.mockReturnValue(new Promise(() => {}))
  await render('000001.SZ')
  expect(host.querySelector('[data-markers]')?.textContent).toBe('')
  expect(host.textContent).toContain('读取财报版本')
  await act(async () => client.setQueryData(QK.earningsBreakout('000001.SZ', range.start, range.end), {
    symbol: '000001.SZ', source: 'fuyao', status: 'data_insufficient', reports: 1, eligible_reports: 0, markers: [], events: [],
  }))
  await settle()
  expect(host.textContent).toContain('没有满足条件且当时可用的财报依据')
  expect(host.querySelector('[data-markers]')?.textContent).toBe('')
  await clickPill('业绩断层')
  expect(host.textContent).not.toContain('没有满足条件且当时可用的财报依据')
})

it('无版本、无日历和读取失败有明确提示，出错后清除旧指标', async () => {
  mocks.daily.mockResolvedValue(response('600000.SH', 10.25))
  mocks.earnings.mockResolvedValue({ symbol: '600000.SH', status: 'no_versions', source: 'fuyao', reports: 0, eligible_reports: 0, markers: [], events: [] })
  await render()
  await clickPill('业绩断层')
  expect(host.textContent).toContain('请在数据页同步财务')
  await act(async () => client.setQueryData(QK.earningsBreakout('600000.SH', range.start, range.end), {
    symbol: '600000.SH', status: 'no_calendar', reports: 0, eligible_reports: 0, markers: [], events: [],
  }))
  await settle()
  expect(host.textContent).toContain('缺少本地交易日历')
  mocks.earnings.mockRejectedValue(new Error('本地数据损坏'))
  await act(async () => { await client.invalidateQueries({ queryKey: QK.earningsBreakout('600000.SH', range.start, range.end) }) })
  await settle()
  expect(host.textContent).toContain('业绩断层读取失败')
  expect(host.querySelector('[data-markers]')?.textContent).toBe('')
})

it('缺口区域默认显示且无开关，回补、切股和出错时清除旧价格带', async () => {
  mocks.daily.mockResolvedValue(response('600000.SH', 10.25))
  const report = { symbol: '600000.SH', status: 'ready', history_start: '2025-01-01', as_of: range.end,
    gaps: [{ date: '2025-12-01', direction: 'up', lower: 10, upper: 11, original_lower: 10, original_upper: 12 }] }
  mocks.gaps.mockResolvedValue(report)
  await render()
  expect(mocks.gaps).toHaveBeenCalledWith('600000.SH', range.end)
  expect([...host.querySelectorAll('button')].some(button => button.textContent === '缺口区域')).toBe(false)
  expect(host.querySelector('[data-gaps]')?.textContent).toContain('"upper":11')
  expect(host.textContent).toContain('未回补 1 个')
  expect(mocks.earnings).not.toHaveBeenCalled()
  await act(async () => client.setQueryData(QK.klineGaps('600000.SH', range.end), {
    ...report, gaps: [{ ...report.gaps[0], upper: 10.5 }],
  }))
  await settle()
  expect(host.querySelector('[data-gaps]')?.textContent).toContain('"upper":10.5')
  await act(async () => client.setQueryData(QK.klineGaps('600000.SH', range.end), { ...report, gaps: [] }))
  await settle()
  expect(host.querySelector('[data-gaps]')?.textContent).toBe('')
  expect(host.textContent).toContain('未回补 0 个')
  await act(async () => client.setQueryData(QK.klineGaps('600000.SH', range.end), report))
  await settle()
  expect(host.querySelector('[data-gaps]')?.textContent).toContain('"upper":11')
  mocks.daily.mockResolvedValue(response('000001.SZ', 20.5))
  mocks.gaps.mockRejectedValue(new Error('bad price data'))
  await render('000001.SZ')
  expect(host.querySelector('[data-gaps]')?.textContent).toBe('')
  expect(host.textContent).toContain('缺口区域读取失败')
})

it('日周月独立读取，保留历史截止且不把日线标记、缺口和线段带入周月', async () => {
  mocks.daily.mockResolvedValue({ ...response('600000.SH', 10.25), rows: [{ ...response('600000.SH', 10.25).rows[0], signal_limit_up: true }] })
  mocks.gaps.mockResolvedValue({ symbol: '600000.SH', status: 'ready', gaps: [{ date: '2026-01-05', lower: 10, upper: 11 }] })
  let resolveWeekly: (value: unknown) => void = () => {}
  mocks.boxes.mockImplementation((symbol: string, period: string) => period === 'weekly'
    ? new Promise(resolve => { resolveWeekly = resolve })
    : Promise.resolve({ symbol, period, status: 'insufficient_data', rows: [], boxes: [], diagnostics: [] }))
  await act(async () => root.render(<QueryClientProvider client={client}><StockDailyKChart symbol="600000.SH" dateRange={range}
    markers={[{ date: '2026-01-05', kind: 'buy', label: '买' }]} ranges={[{ start: '2026-01-05', end: '2026-01-06', color: '#123' }]}
    priceLines={[{ value: 10, label: '日线支撑' }]} /></QueryClientProvider>))
  await settle()
  expect(host.querySelector('[data-markers]')?.textContent).toContain('板')
  expect(host.querySelector('[data-chart]')?.getAttribute('data-ranges')).toBe('1')
  await clickPill('周K')
  expect(host.querySelector('[data-chart]')).toBeNull()
  expect(mocks.boxes).toHaveBeenCalledWith('600000.SH', 'weekly', { start: '2021-01-31', end: range.end })
  await act(async () => resolveWeekly({ symbol: '600000.SH', period: 'weekly', status: 'ready', boxes: [], diagnostics: ['当前周K尚未完成'],
    rows: [{ date: '2026-01-30', open: 10, high: 15, low: 8, close: 14, bull_bear_line: 12, is_complete: false }] }))
  await settle()
  expect(host.querySelector('[data-chart]')?.getAttribute('data-rows')).toContain('2026-01-30')
  expect(host.querySelector('[data-markers]')?.textContent).toBe('')
  expect(host.querySelector('[data-gaps]')?.textContent).toBe('')
  expect(host.querySelector('[data-chart]')?.getAttribute('data-ranges')).toBe('0')
  expect(host.querySelector('[data-chart]')?.getAttribute('data-lines')).toBe('0')
  expect(host.textContent).toContain('当前周K尚未完成')
  await clickPill('月K')
  expect(mocks.boxes).toHaveBeenCalledWith('600000.SH', 'monthly', { start: '2011-01-31', end: range.end })
  expect(host.textContent).toContain('暂无本地月K数据')
  await clickPill('日K')
  expect(host.querySelector('[data-markers]')?.textContent).toContain('买')
  expect(host.querySelector('[data-chart]')?.getAttribute('data-ranges')).toBe('1')
})

it('箱体默认关闭且不请求，手动开启后筛选所选结构并展示详情，关闭后隐藏', async () => {
  mocks.daily.mockResolvedValue(response('600000.SH', 10.25))
  const base = { period: 'daily', lower: 10, upper: 12, width: 2, width_pct: 20, start_date: '2026-01-01', end_date: '2026-01-31',
    observed_until: '2026-01-31', confirmed_date: '2026-01-22', duration_bars: 22, status: 'confirmed',
    upper_touches: [{ date: '2026-01-05', price: 12, confirmed_date: '2026-01-07' }],
    lower_touches: [{ date: '2026-01-09', price: 10, confirmed_date: null }], missing_conditions: [], parent_id: null,
    breakout: { status: 'returned', direction: 'up', first_date: '2026-01-28', confirmed_date: null },
    events: [{ type: 'returned', date: '2026-01-29', direction: 'up' }] }
  mocks.boxes.mockResolvedValue({ symbol: '600000.SH', period: 'daily', status: 'ready', history_start: '2020-01-01', as_of: range.end, diagnostics: [], rows: [],
    boxes: [{ ...base, id: 'small', level: 'short', parent_id: 'large' }, { ...base, id: 'large', level: 'long', lower: 8, upper: 15, duration_bars: 70 }] })
  await render()
  expect(mocks.boxes).not.toHaveBeenCalled()
  expect([...host.querySelectorAll('button')].find(button => button.textContent === '箱体')?.getAttribute('aria-pressed')).toBe('false')
  expect(host.querySelector('[data-boxes]')?.textContent).toBe('')
  expect(host.querySelector('[aria-label="箱体大小"]')).toBeNull()
  await clickPill('箱体')
  expect(mocks.boxes).toHaveBeenCalledWith('600000.SH', 'daily', range)
  expect(host.querySelector('[data-boxes]')?.textContent).toBe('small')
  await clickPill('箱体详情（2）')
  expect(host.textContent).toContain('宽度 20.00%')
  expect(host.textContent).toContain('首次确认 2026-01-22')
  expect(host.textContent).toContain('2026-01-07 确认')
  expect(host.textContent).toContain('嵌套父箱：大箱体 8.00–15.00')
  expect(host.textContent).toContain('2026-01-29 向上回到箱内')
  const size = host.querySelector<HTMLSelectElement>('[aria-label="箱体大小"]')!
  await act(async () => { size.value = 'long'; size.dispatchEvent(new Event('change', { bubbles: true })) })
  expect(host.querySelector('[data-boxes]')?.textContent).toBe('large')
  const status = host.querySelector<HTMLSelectElement>('[aria-label="箱体状态"]')!
  await act(async () => { status.value = 'candidate'; status.dispatchEvent(new Event('change', { bubbles: true })) })
  expect(host.querySelector('[data-boxes]')?.textContent).toBe('')
  expect(host.textContent).toContain('当前范围与筛选条件下没有箱体')
  await act(async () => { status.value = 'all'; status.dispatchEvent(new Event('change', { bubbles: true })) })
  expect(host.querySelector('[data-boxes]')?.textContent).toBe('large')
  await clickPill('箱体')
  expect(host.querySelector('[data-boxes]')?.textContent).toBe('')
  expect(host.querySelector('[aria-label="箱体大小"]')).toBeNull()
})
