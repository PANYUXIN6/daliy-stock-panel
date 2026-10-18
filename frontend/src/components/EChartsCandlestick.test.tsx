// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { EChartsCandlestick, OVERLAY_INDICATORS, type OHLC } from './EChartsCandlestick'

const chart = vi.hoisted(() => ({
  handlers: {} as Record<string, (event?: any) => void>,
  setOption: vi.fn(),
  on: vi.fn(),
  off: vi.fn(),
  dispatchAction: vi.fn(),
  resize: vi.fn(),
  dispose: vi.fn(),
  getZr: () => ({ on: vi.fn(), off: vi.fn() }),
  getOption: () => ({ dataZoom: [{ start: 0, end: 100 }] }),
  containPixel: () => false,
  convertFromPixel: () => [0, 0],
}))

vi.mock('echarts', async importOriginal => ({
  ...await importOriginal<typeof import('echarts')>(), init: () => chart,
}))

// 超过 COMPACT_THRESHOLD(60): 切标的后 dataZoom 一响就会走紧凑态转换, 进而重写 markPoint
const DAYS = Array.from({ length: 62 }, (_, i) =>
  new Date(Date.UTC(2024, 0, 1 + i)).toISOString().slice(0, 10))
const rows = (base: number): OHLC[] =>
  DAYS.map(date => ({ date, open: base, high: base + 1, low: base - 1, close: base, volume: 1 }))

let host: HTMLDivElement
let root: ReturnType<typeof createRoot>

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  chart.handlers = {}
  chart.setOption.mockClear()
  chart.on.mockImplementation((name: string, callback: (event?: any) => void) => {
    chart.handlers[name] = callback
  })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
})

/** 最后一次 setOption 里 K 线的 markPoint (买卖箭头来源) */
function lastMarkPoint(): string {
  const option = chart.setOption.mock.calls.at(-1)?.[0] as any
  const k = option?.series?.find((s: any) => s.name === 'K')
  return JSON.stringify(k?.markPoint ?? null)
}

/** 模拟竖虚线命中某根 K 线 (鼠标在数据区内移动时 echarts 派发的事件) */
async function hoverCandle(index: number) {
  await act(async () => { chart.handlers.updateAxisPointer?.({ axesInfo: [{ value: index }] }) })
}

it('多空线按相邻值逐段变色，平走为红，悬停显示数值且开关独立于均线', async () => {
  expect(OVERLAY_INDICATORS).toContainEqual({ key: 'bull_bear_line', label: '多空线' })
  const data = rows(10).slice(0, 5).map((row, index) => ({
    ...row, bull_bear_line: [10, 12, 9, 9, null][index],
  }))
  const render = async (activeIndicators: string[]) => {
    await act(async () => root.render(
      <EChartsCandlestick data={data} showMA={false} activeIndicators={activeIndicators} />,
    ))
  }
  await render(['bull_bear_line'])
  const option = chart.setOption.mock.calls.at(-1)?.[0] as any
  const line = option.series.find((series: any) => series.name === '多空线')
  expect(line.data).toHaveLength(3)
  const strokes = line.data.map((segment: number[]) => line.renderItem({}, {
    value: (index: number) => segment[index], coord: (value: number[]) => value,
  }).style)
  expect(strokes).toEqual([
    { stroke: '#FF0000', lineWidth: 3 },
    { stroke: '#08FF16', lineWidth: 3 },
    { stroke: '#FF0000', lineWidth: 3 },
  ])
  await hoverCandle(2)
  expect(host.textContent).toContain('多空线:9.00')
  expect([...host.querySelectorAll('span')].find(span => span.textContent === '多空线:9.00')?.style.color)
    .toBe('rgb(8, 255, 22)')
  await hoverCandle(4)
  expect(host.textContent).toContain('多空线:—')
  await render([])
  expect(host.textContent).not.toContain('多空线:')
  expect((chart.setOption.mock.calls.at(-1)?.[0] as any).series.some((series: any) => series.name === '多空线')).toBe(false)
})

it('多空线切股和更新实时尾部后使用当前数值，缺失值不跨空档连线', async () => {
  const render = async (symbol: string, values: (number | null)[]) => {
    await act(async () => root.render(<EChartsCandlestick symbol={symbol} activeIndicators={['bull_bear_line']}
      data={rows(10).slice(0, values.length).map((row, index) => ({ ...row, bull_bear_line: values[index] }))} />))
  }
  await render('600000.SH', [10, 11, 12])
  await render('000001.SZ', [100, null, 90])
  expect(host.textContent).toContain('多空线:90.00')
  expect((chart.setOption.mock.calls.at(-1)?.[0] as any).series.find((series: any) => series.name === '多空线').data).toEqual([])
  await render('000001.SZ', [100, 95, 96])
  expect(host.textContent).toContain('多空线:96.00')
})

it('切标的后响应 dataZoom 仍用当前标的的买卖标记, 不回退到上一只', async () => {
  const render = async (symbol: string, data: OHLC[], markerLabel: string, markerDate: string) => {
    await act(async () => root.render(
      <EChartsCandlestick
        data={data}
        markers={[{ date: markerDate, kind: 'buy', label: markerLabel }]}
        symbol={symbol}
        height={400}
        showInfoBar={false}
        visibleBars="all"
        activeIndicators={['vol']}
      />,
    ))
  }

  await render('600000', rows(10), 'A-BUY', DAYS[10])
  await render('000001', rows(100), 'B-SELL', DAYS[20])

  // 切换标的后图表实例被复用, dataZoom 监听仍是最初注册的那个; 首次 setOption 已是当前标的
  expect(lastMarkPoint()).toContain('B-SELL')

  await act(async () => { chart.handlers.dataZoom?.() })

  expect(lastMarkPoint()).toContain('B-SELL')
  expect(lastMarkPoint()).not.toContain('A-BUY')
})

it('切股后鼠标未离开图表, 竖虚线重新命中即恢复「至今/周期」', async () => {
  const render = async (symbol: string) => {
    await act(async () => root.render(
      <EChartsCandlestick data={rows(10)} symbol={symbol} height={400} visibleBars="all" />,
    ))
  }

  await render('600000')
  const surface = host.firstElementChild as HTMLElement
  await act(async () => { surface.dispatchEvent(new MouseEvent('mouseenter')) })
  await hoverCandle(30)
  expect(host.textContent).toContain('至今')

  // 切股: 上一只的悬停上下文作废, 清掉「至今/周期」
  await render('000001')
  expect(host.textContent).not.toContain('至今')

  // 鼠标没离开图表区 (只是切股), 竖虚线重新命中即恢复, 不靠 mouseenter
  await hoverCandle(20)
  expect(host.textContent).toContain('至今')
  expect(host.textContent).toContain('周期')

  // 移出图表区 → 重新收起
  await act(async () => { surface.dispatchEvent(new MouseEvent('mouseleave')) })
  expect(host.textContent).not.toContain('至今')
})


it('缺口与中点保留颜色、形状、间距、点击日期和悬浮说明，缩放后仍一致', async () => {
  const markers = [
    { date: DAYS[20], kind: 'buy' as const, label: '缺口', description: '缺口回踩\n当日为阳线', color: '#FACC15', symbol: 'arrow' as const, offset: 12 },
    { date: DAYS[20], kind: 'buy' as const, label: '中点', description: '中点回踩\n最低价<断层当天最高价', color: '#A78BFA', symbol: 'diamond' as const, offset: 56 },
  ]
  await act(async () => root.render(<EChartsCandlestick data={rows(10)} markers={markers} visibleBars="all" />))
  const check = () => {
    const option = chart.setOption.mock.calls.at(-1)?.[0] as any
    const points = option.series.find((s: any) => s.name === 'K').markPoint.data
    expect(points.map((p: any) => [p.name, p.symbol, p.symbolOffset, p.itemStyle.color])).toEqual([
      [DAYS[20], 'arrow', [0, 12], '#FACC15'], [DAYS[20], 'diamond', [0, 56], '#A78BFA'],
    ])
    expect(points.map((p: any) => p.label.formatter)).toEqual(['缺口', '中点'])
    expect(points.map((p: any) => p.tooltip.trigger)).toEqual(['item', 'item'])
    expect(points.map((p: any) => p.tooltip.formatter())).toEqual([
      '缺口回踩<br/>当日为阳线', '中点回踩<br/>最低价&lt;断层当天最高价',
    ])
  }
  check()
  await act(async () => { chart.handlers.dataZoom?.() })
  check()
})

it('价格带使用剩余上下沿，旧缺口从窗口左侧延伸，回补后移除且不覆盖已有区间', async () => {
  const render = async (priceGaps: any[]) => {
    await act(async () => root.render(<EChartsCandlestick data={rows(10)} priceGaps={priceGaps}
      ranges={[{ start: DAYS[10], end: DAYS[12], label: '原有区间' }]} />))
  }
  await render([
    { date: '2023-12-01', direction: 'up', lower: 8, upper: 8.5, original_lower: 8, original_upper: 9 },
    { date: DAYS[30], direction: 'down', lower: 11.5, upper: 12, original_lower: 11, original_upper: 12 },
    { date: DAYS[40], direction: 'up', lower: 10, upper: 10, original_lower: 10, original_upper: 11 },
    { date: '2027-01-01', direction: 'up', lower: 8, upper: 9, original_lower: 8, original_upper: 9 },
  ])
  const option = chart.setOption.mock.calls.at(-1)?.[0] as any
  const areas = option.series.find((s: any) => s.name === '缺口区域').markArea
  expect(areas.silent).toBe(false)
  expect(areas.data).toHaveLength(2)
  expect(areas.data.map((a: any) => [a[0].xAxis, a[0].yAxis, a[1].xAxis, a[1].yAxis])).toEqual([
    [DAYS[0], 8, 'max', 8.5], [DAYS[30], 11.5, 'max', 12],
  ])
  expect(areas.data[0][0].tooltip.formatter()).toContain('未回补区间：8.00–8.50')
  expect(option.series.find((s: any) => s.name === 'K').markArea.data[0][0].name).toBe('原有区间')
  await render([])
  expect((chart.setOption.mock.calls.at(-1)?.[0] as any).series.some((s: any) => s.name === '缺口区域')).toBe(false)
})
