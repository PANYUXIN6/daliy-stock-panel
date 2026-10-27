// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Indices } from './Indices'

type MinuteResponse = {
  symbol: string
  date: string
  rows: { datetime: string; close: number }[]
}
type DailyResponse = {
  symbol: string
  rows: { date: string; open: number; high: number; low: number; close: number; volume: number }[]
}
const fixtures = vi.hoisted(() => ({
  pending: [] as { symbol: string; day: string; resolve: (value: MinuteResponse) => void }[],
  pendingDaily: [] as { symbol: string; resolve: (value: DailyResponse) => void }[],
  deferDaily: false,
  mounts: 0,
  minuteCap: true,
  dailyCap: true,
  sync: vi.fn(),
  boxes: vi.fn(),
}))

vi.mock('@/lib/api', () => ({
  api: {
    syncIndexDaily: fixtures.sync,
    klineBoxes: fixtures.boxes,
    indexQuotes: async () => ({ rows: [] }),
    indexDaily: (symbol: string) => fixtures.deferDaily
      ? new Promise<DailyResponse>(resolve => fixtures.pendingDaily.push({ symbol, resolve }))
      : Promise.resolve({
        symbol,
        rows: ['2026-09-09', '2026-09-10', '2026-09-11'].map(date => ({
          date, open: 10, high: 12, low: 9, close: 11, volume: 1,
        })),
      }),
    indexMinute: (symbol: string, day: string) => new Promise<MinuteResponse>(resolve => {
      fixtures.pending.push({ symbol, day, resolve })
    }),
  },
}))
vi.mock('@/lib/useSharedQueries', () => ({
  useCapabilityMatrix: () => ({ data: undefined }),
  useCapabilities: () => ({ data: { capabilities: { 'kline.minute.batch': fixtures.minuteCap, 'kline.daily.batch': fixtures.dailyCap } } }),
}))
vi.mock('@/components/EChartsCandlestick', () => ({
  EChartsCandlestick: ({ data, onDateClick, symbol }: {
    data: { date: string; close: number }[]; onDateClick: (date: string) => void; symbol: string
  }) => <div data-daily-chart data-symbol={symbol} data-close={data.at(-1)?.close}>{data.map(row => (
    <button key={row.date} data-day={row.date} onClick={() => onDateClick(row.date)}>{row.date}</button>
  ))}</div>,
}))
vi.mock('@/components/EChartsIntraday', () => ({
  EChartsIntraday: ({ data, date }: { data: MinuteResponse['rows']; date: string }) => {
    const [mount] = useState(() => ++fixtures.mounts)
    return <div data-chart data-mount={mount}>{`${date}|${data[0].datetime}|${data[0].close}`}</div>
  },
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  fixtures.pending.length = 0
  fixtures.pendingDaily.length = 0
  fixtures.deferDaily = false
  fixtures.mounts = 0
  fixtures.minuteCap = true
  fixtures.dailyCap = true
  fixtures.sync.mockReset().mockResolvedValue({ job_id: 'index-job', reused: false })
  fixtures.boxes.mockReset().mockImplementation(async (symbol: string, period: string) => ({ symbol, period, status: 'ready', rows: [], boxes: [], diagnostics: [] }))
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: {
    retry: false, staleTime: Infinity, gcTime: Infinity,
  } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

// React Query batches observer notifications on the next timer tick.
async function settle() {
  for (let i = 0; i < 5; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

async function resolveDay(day: string, close: number) {
  const index = fixtures.pending.findIndex(call => call.day === day)
  expect(index).toBeGreaterThanOrEqual(0)
  const [call] = fixtures.pending.splice(index, 1)
  await act(async () => call.resolve({
    symbol: call.symbol, date: day, rows: [{ datetime: `${day}T09:35:00`, close }],
  }))
  await settle()
}

async function clickDay(day: string) {
  const button = host.querySelector<HTMLButtonElement>(`[data-day="${day}"]`)
  expect(button).not.toBeNull()
  await act(async () => button!.click())
  await settle()
}

it('keeps selected dates isolated across delayed, out-of-order and cached responses', async () => {
  await act(async () => root.render(
    <MemoryRouter><QueryClientProvider client={client}><Indices /></QueryClientProvider></MemoryRouter>,
  ))
  await settle()
  expect(fixtures.boxes).not.toHaveBeenCalled()
  expect([...host.querySelectorAll('button')].find(button => button.textContent === '箱体')?.getAttribute('aria-pressed')).toBe('false')
  const chart = () => host.querySelector<HTMLElement>('[data-chart]')
  await resolveDay('2026-09-11', 11)
  expect(chart()?.textContent).toBe('2026-09-11|2026-09-11T09:35:00|11')

  await clickDay('2026-09-10')
  expect(chart()).toBeNull()
  await clickDay('2026-09-09')
  await resolveDay('2026-09-10', 10)
  expect(chart()).toBeNull()
  await resolveDay('2026-09-09', 9)
  expect(chart()?.textContent).toBe('2026-09-09|2026-09-09T09:35:00|9')

  const mount9 = chart()?.dataset.mount
  await clickDay('2026-09-11')
  expect(chart()?.textContent).toBe('2026-09-11|2026-09-11T09:35:00|11')
  expect(chart()?.dataset.mount).not.toBe(mount9)

  const mount11 = chart()?.dataset.mount
  await act(async () => { void client.invalidateQueries({ queryKey: ['index-minute'] }) })
  await settle()
  expect(chart()?.textContent).toBe('2026-09-11|2026-09-11T09:35:00|11')
  await resolveDay('2026-09-11', 12)
  expect(chart()?.textContent).toBe('2026-09-11|2026-09-11T09:35:00|12')
  expect(chart()?.dataset.mount).toBe(mount11)
})

it('shows saved local index minutes when the provider minute capability is unavailable', async () => {
  fixtures.minuteCap = false
  fixtures.dailyCap = false
  await act(async () => root.render(<MemoryRouter><QueryClientProvider client={client}><Indices /></QueryClientProvider></MemoryRouter>))
  await settle()
  await resolveDay('2026-09-11', 11)
  expect(host.querySelector('[data-chart]')?.textContent).toBe('2026-09-11|2026-09-11T09:35:00|11')
  const refresh = [...host.querySelectorAll('button')].find(b => b.textContent === '刷新')!
  await act(async () => refresh.click()); await settle()
  await resolveDay('2026-09-11', 12)
  expect(host.querySelector('[data-chart]')?.textContent).toBe('2026-09-11|2026-09-11T09:35:00|12')
  expect(fixtures.sync).not.toHaveBeenCalled()
})

it('isolates daily prices during index changes and keeps the current index during a local refresh', async () => {
  fixtures.deferDaily = true
  await act(async () => root.render(
    <MemoryRouter><QueryClientProvider client={client}><Indices /></QueryClientProvider></MemoryRouter>,
  ))
  await settle()
  const chart = () => host.querySelector<HTMLElement>('[data-daily-chart]')
  const resolveDaily = async (symbol: string, close: number) => {
    const index = fixtures.pendingDaily.findIndex(call => call.symbol === symbol)
    expect(index).toBeGreaterThanOrEqual(0)
    const [call] = fixtures.pendingDaily.splice(index, 1)
    await act(async () => call.resolve({
      symbol,
      rows: [{ date: '2026-09-11', open: close, high: close, low: close, close, volume: 1 }],
    }))
    await settle()
  }
  const selectIndex = async (name: string) => {
    const button = [...host.querySelectorAll<HTMLButtonElement>('aside button')]
      .find(item => item.textContent?.includes(name))
    expect(button).toBeDefined()
    await act(async () => button!.click())
    await settle()
  }

  await resolveDaily('000001.SH', 3010)
  expect(chart()?.dataset.symbol).toBe('000001.SH')
  expect(chart()?.dataset.close).toBe('3010')

  await selectIndex('深证成指')
  expect(chart()).toBeNull()
  expect(host.textContent).toContain('日K加载中')
  await resolveDaily('399001.SZ', 10050)
  expect(chart()?.dataset.symbol).toBe('399001.SZ')
  expect(chart()?.dataset.close).toBe('10050')

  await selectIndex('上证指数')
  expect(chart()?.dataset.symbol).toBe('000001.SH')
  expect(chart()?.dataset.close).toBe('3010')
  await act(async () => { void client.invalidateQueries({ queryKey: ['index-daily'] }) })
  await settle()
  expect(chart()?.dataset.close).toBe('3010')
  await resolveDaily('000001.SH', 3011)
  expect(chart()?.dataset.close).toBe('3011')
})

it('周月线使用所选指数的聚合K线，点击最后交易日打开分时，切换指数不透传旧数据', async () => {
  fixtures.boxes.mockImplementation(async (symbol: string, period: string) => ({ symbol, period, status: 'ready', boxes: [], diagnostics: ['未完成周期仅供展示'],
    rows: [{ date: period === 'weekly' ? '2026-09-11' : '2026-09-30', open: 3000, high: 3100, low: 2900, close: 3050, volume: 12 }] }))
  await act(async () => root.render(<MemoryRouter><QueryClientProvider client={client}><Indices /></QueryClientProvider></MemoryRouter>))
  await settle()
  const click = async (label: string) => {
    const button = [...host.querySelectorAll<HTMLButtonElement>('button')].find(item => item.textContent === label)!
    await act(async () => button.click()); await settle()
  }
  await click('周K')
  expect(fixtures.boxes).toHaveBeenCalledWith('000001.SH', 'weekly', expect.objectContaining({ start: expect.stringMatching(/^2021-/) }))
  expect(host.querySelector<HTMLElement>('[data-daily-chart]')?.dataset.close).toBe('3050')
  expect(host.textContent).toContain('未完成周期仅供展示')
  const start = host.querySelector<HTMLInputElement>('[aria-label="K线起始日期"]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(start, '2024-01-01')
    start.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await settle()
  expect(start.value).toBe('2024-01-01')
  expect(fixtures.boxes).toHaveBeenCalledWith('000001.SH', 'weekly', expect.objectContaining({ start: '2024-01-01' }))
  await clickDay('2026-09-11')
  await resolveDay('2026-09-11', 11)
  expect(host.querySelector('[data-chart]')?.textContent).toContain('2026-09-11|')
  await click('月K')
  expect(fixtures.boxes).toHaveBeenCalledWith('000001.SH', 'monthly', expect.objectContaining({ start: expect.stringMatching(/^2011-/) }))
  await clickDay('2026-09-30')
  await resolveDay('2026-09-30', 30)
  expect(host.querySelector('[data-chart]')?.textContent).toContain('2026-09-30|')
  fixtures.boxes.mockReturnValue(new Promise(() => {}))
  const next = [...host.querySelectorAll<HTMLButtonElement>('aside button')].find(item => item.textContent?.includes('深证成指'))!
  await act(async () => next.click()); await settle()
  expect(host.querySelector('[data-daily-chart]')).toBeNull()
  expect(host.querySelector('[data-chart]')).toBeNull()
})

it('清空日期保留最后有效范围，起点晚于截止时提示并停止查询', async () => {
  await act(async () => root.render(<MemoryRouter><QueryClientProvider client={client}><Indices /></QueryClientProvider></MemoryRouter>))
  await settle()
  const end = host.querySelector<HTMLInputElement>('[aria-label="K线截止日期"]')!
  const originalEnd = end.value
  const change = async (input: HTMLInputElement, value: string) => {
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value)
      input.dispatchEvent(new Event('change', { bubbles: true }))
    })
    await settle()
  }
  const switchPeriod = async (label: string) => {
    await act(async () => [...host.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === label)!.click())
    await settle()
  }
  await change(end, '')
  expect(end.value).toBe(originalEnd)
  await switchPeriod('周K')
  await change(end, '')
  expect(end.value).toBe(originalEnd)
  await switchPeriod('月K')
  expect(fixtures.boxes).toHaveBeenLastCalledWith('000001.SH', 'monthly', expect.objectContaining({ end: originalEnd }))
  const count = fixtures.boxes.mock.calls.length
  await change(host.querySelector<HTMLInputElement>('[aria-label="K线起始日期"]')!, '2099-01-01')
  expect(host.querySelector('[role="alert"]')?.textContent).toBe('起始日期不能晚于截止日期')
  expect(fixtures.boxes).toHaveBeenCalledTimes(count)
})
