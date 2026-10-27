// @vitest-environment jsdom
import { act, type ComponentProps } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { KlineRow } from '@/lib/api'
import { AnalysisKChart, LEVEL_GROUPS, type LevelType, type PriceLevel } from './AnalysisKChart'

const chart = vi.hoisted(() => ({
  handlers: {} as Record<string, (event?: unknown) => void>,
  setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn(), on: vi.fn(),
}))
vi.mock('echarts', () => ({ init: () => chart }))

interface SeriesOption {
  name: string
  type: string
  data: unknown[]
  lineStyle?: { opacity: number }
  zlevel?: number
  renderItem?: (params: unknown, api: {
    value: (index: number) => number
    coord: (point: number[]) => number[]
  }) => { style: { stroke: string; lineWidth: number } }
}
interface ChartOption {
  series: SeriesOption[]
  tooltip: { show: boolean }
}

let host: HTMLDivElement
let root: ReturnType<typeof createRoot>

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  chart.handlers = {}
  chart.setOption.mockClear()
  chart.on.mockImplementation((name: string, callback: (event?: unknown) => void) => {
    chart.handlers[name] = callback
  })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
})

function rows(values: (number | null | undefined)[]): KlineRow[] {
  return values.map((value, index) => ({
    date: `2026-10-${String(index + 1).padStart(2, '0')}`,
    open: 20, high: 22, low: 19, close: 21, volume: 100,
    bull_bear_line: value,
  }))
}
async function render(values: (number | null | undefined)[], props: Partial<ComponentProps<typeof AnalysisKChart>> = {}) {
  await act(async () => root.render(<AnalysisKChart rows={rows(values)} {...props} />))
}
function lastOption(): ChartOption {
  return chart.setOption.mock.calls.at(-1)?.[0] as ChartOption
}
function indicatorButton(): HTMLButtonElement {
  const button = [...host.querySelectorAll('button')].find(node => node.textContent === '多空线')
  if (!button) throw new Error('未显示多空线开关')
  return button
}
function indicatorSeries(): SeriesOption {
  const series = lastOption().series.find(series => series.name === '多空线')
  if (!series) throw new Error('未绘制多空线')
  return series
}

it('没有关键价位时仍默认开启多空线，直接绘制后端值且上下行和平走逐段变色', async () => {
  await render([10, 12, 9, 9])
  expect(indicatorButton().getAttribute('aria-pressed')).toBe('true')
  const line = indicatorSeries()
  expect(line.data).toEqual([[0, 10, 1, 12, 0], [1, 12, 2, 9, 1], [2, 9, 3, 9, 0]])
  expect(line.data.map(segment => line.renderItem!({}, {
    value: index => (segment as number[])[index], coord: point => point,
  }).style)).toEqual([
    { stroke: '#FF0000', lineWidth: 3 },
    { stroke: '#08FF16', lineWidth: 3 },
    { stroke: '#FF0000', lineWidth: 3 },
  ])
  expect(host.textContent).toContain('多空线:9.00')
  expect(lastOption().tooltip.show).toBe(false)
  expect(LEVEL_GROUPS.some(group => String(group.key) === 'bull_bear_line')).toBe(false)
})

it('多空线开关独立关闭与重新开启，图中和指标数值都不残留', async () => {
  await render([10, 12])
  await act(async () => indicatorButton().click())
  expect(indicatorButton().getAttribute('aria-pressed')).toBe('false')
  expect(lastOption().series.some(series => series.name === '多空线')).toBe(false)
  expect(host.textContent).not.toContain('多空线:')
  expect(lastOption().series.map(series => series.name)).toEqual(['K', '成交量'])
  await act(async () => indicatorButton().click())
  expect(indicatorButton().getAttribute('aria-pressed')).toBe('true')
  expect(indicatorSeries().data).toEqual([[0, 10, 1, 12, 0]])
})

it('切换股票数据和更新尾部时使用当前最新指标值，缺失时不回找旧值', async () => {
  await render([10, 12])
  expect(host.textContent).toContain('多空线:12.00')
  await render([100, 90])
  expect(host.textContent).toContain('多空线:90.00')
  expect(host.textContent).not.toContain('多空线:12.00')
  expect(indicatorSeries().data).toEqual([[0, 100, 1, 90, 1]])
  expect([...host.querySelectorAll('span')].find(span => span.textContent === '多空线:90.00')?.style.color)
    .toBe('rgb(8, 255, 22)')
  await render([100, 96])
  expect(host.textContent).toContain('多空线:96.00')
  expect(indicatorSeries().data).toEqual([[0, 100, 1, 96, 1]])
  await render([100, null])
  expect(host.textContent).toContain('多空线:—')
  expect(indicatorSeries().data).toEqual([])
  await render([undefined, undefined])
  expect(host.textContent).toContain('多空线:—')
  expect(indicatorSeries().data).toEqual([])
})

it('空值和非有限值不跨缺口连线，末值为零正常显示', async () => {
  await render([10, null, 12, undefined, 13, Number.NaN, Number.POSITIVE_INFINITY, 14, 15])
  expect(indicatorSeries().data).toEqual([[7, 14, 8, 15, 0]])
  await render([1, 0])
  expect(host.textContent).toContain('多空线:0.00')
  expect(indicatorSeries().data).toEqual([[0, 1, 1, 0, 1]])
  await render([1, Number.NaN])
  expect(host.textContent).toContain('多空线:—')
  await render([])
  expect(host.textContent).toContain('多空线:—')
})

it('叠加多空线后关键价位和通道高亮仍对应原来的线，关闭指标不改变其他叠加', async () => {
  const levels: Record<LevelType, PriceLevel[]> = {
    sr: [], pivot: [], extreme: [], boll: [],
    keltner_s: [], keltner_m: [], keltner_l: [],
    atr_stop: [], gap: [], fib: [], round: [],
  }
  levels.sr = [{ value: 11, label: '支撑位', type: 'sr', side: 'support' }]
  await render([10, 12], {
    levels, seriesDates: ['2026-10-01', '2026-10-02'],
    series: { keltner_s: { upper: [13, 14], lower: [8, 9] } },
  })
  const names = ['K', '成交量', '支撑位', 'Keltner短上', 'Keltner短下']
  expect(lastOption().series.map(series => series.name)).toEqual([...names, '多空线'])
  await act(async () => chart.handlers.mouseover({ componentType: 'series', seriesIndex: 2 }))
  expect(lastOption().series[2].zlevel).toBe(10)
  expect(lastOption().series[3].lineStyle?.opacity).toBe(0.12)
  await act(async () => chart.handlers.mouseover({ componentType: 'series', seriesIndex: 3 }))
  expect(lastOption().series[2].zlevel).toBe(0)
  expect(lastOption().series[3].zlevel).toBe(10)
  expect(lastOption().series[4].zlevel).toBe(10)
  await act(async () => indicatorButton().click())
  expect(lastOption().series.map(series => series.name)).toEqual(names)
  expect(lastOption().series[3].zlevel).toBe(10)
  expect(lastOption().series[4].zlevel).toBe(10)
})

it('后端仍返回 ATR 时只展示 Keltner 通道，开关同步控制曲线与价位统计', async () => {
  const levels: Record<LevelType, PriceLevel[]> = {
    sr: [], pivot: [], extreme: [], boll: [],
    keltner_s: [{ value: 24, label: 'Keltner短上', type: 'keltner_s', side: 'resistance' }],
    keltner_m: [], keltner_l: [],
    atr_stop: [{ value: 23, label: 'ATR上轨', type: 'atr_stop', side: 'resistance' }],
    gap: [], fib: [], round: [],
  }
  await render([10, 12], {
    levels, seriesDates: ['2026-10-01', '2026-10-02'],
    defaultLevelTypes: ['keltner_s', 'atr_stop'],
    series: {
      keltner_s: { upper: [23, 24], lower: [17, 18] },
      atr: { stop_loss: [18, 19], take_profit: [22, 23] },
    },
  })
  expect(lastOption().series.map(series => series.name)).toEqual(['K', '成交量', 'Keltner短上', 'Keltner短下', '多空线'])
  expect(lastOption().series.find(series => series.name === 'Keltner短上')?.data).toEqual([23, 24])
  expect(host.textContent).toContain('Keltner短上')
  expect(host.textContent).not.toContain('ATR')
  const toggle = [...host.querySelectorAll('button')].find(button => button.textContent?.startsWith('Keltner短期'))!
  await act(async () => toggle.click())
  expect(lastOption().series.map(series => series.name)).toEqual(['K', '成交量', '多空线'])
  expect(host.textContent).not.toContain('Keltner短上')
  expect(host.textContent).not.toContain('ATR')
  await act(async () => toggle.click())
  expect(lastOption().series.find(series => series.name === 'Keltner短下')?.data).toEqual([17, 18])
  expect(host.textContent).toContain('Keltner短上')
})
