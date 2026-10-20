// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { SectorRotation } from '@/lib/api'
import { SectorRotationCard } from './SectorRotationCard'

const mocks = vi.hoisted(() => ({
  rotation: vi.fn(),
  setOption: vi.fn(),
}))

vi.mock('@/lib/api', () => ({ api: {
  sectorRotation: mocks.rotation,
  extDataSchemaAll: async () => ({ items: [] }),
  indexDaily: async () => ({ rows: [] }),
  indexMinute: async () => ({ rows: [] }),
} }))
vi.mock('@/components/Layout', () => ({ CORE_INDEXES: [{ symbol: '000001.SH', name: '上证指数' }] }))
vi.mock('echarts', () => ({
  init: (dom: HTMLDivElement) => ({
    getDom: () => dom,
    isDisposed: () => false,
    dispose: vi.fn(),
    setOption: mocks.setOption,
    resize: vi.fn(),
    on: vi.fn(),
    off: vi.fn(),
    dispatchAction: vi.fn(),
  }),
  getInstanceByDom: () => undefined,
  connect: vi.fn(),
}))

function rotation(matrix: (number | null)[][]): SectorRotation {
  const sectors = ['计算机', '银行', '医药'].slice(0, matrix.length)
  const buckets = ['09:30', '09:35', '09:40']
  return {
    status: 'ok', date: '2024-10-18', as_of: '09:40',
    timeline: buckets.map((time, index) => ({
      time, rotation: 0, leader: sectors[index % sectors.length],
      leader_pct: 0.01, market_pct: 0.005,
    })),
    sectors: [],
    series: { sectors, buckets, matrix },
  }
}

interface ChartOption {
  series: {
    name: string
    data: (number | null | { value: number; label?: unknown })[]
    connectNulls?: boolean
    symbol?: string
  }[]
  tooltip?: { formatter: (params: unknown) => string }
  yAxis: { axisLabel: { formatter: (value: number) => string } }
}

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.setOption.mockClear()
  mocks.rotation.mockReset()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function render(data: SectorRotation) {
  mocks.rotation.mockResolvedValue(data)
  await act(async () => root.render(<QueryClientProvider client={client}>
    <SectorRotationCard kind="industry" />
  </QueryClientProvider>))
  for (let index = 0; index < 5; index++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

function optionFor(name: string): ChartOption {
  const option = mocks.setOption.mock.calls.map(call => call[0] as ChartOption)
    .findLast(option => option.series.some(series => series.name === name))
  if (!option) throw new Error(`找不到图表 ${name}`)
  return option
}

it('稀疏分钟数据提示实际缺口行业数，保留空档且单个数据点可见', async () => {
  await render(rotation([[null, null, 0.01], [0.005, null, 0.006], [0.01, 0.02, 0.03]]))
  expect(host.textContent).toContain('当前展示的 2 个行业存在分钟历史缺口')
  expect(host.textContent).toContain('请补采 2024-10-18 全市场分钟数据')
  const option = optionFor('计算机')
  expect(option.series[0].data).toEqual([null, null, 0.01])
  expect(option.series[0].symbol).toBe('circle')
  expect(option.series[0].connectNulls).toBe(false)
  expect(option.series[1].data).toEqual([0.005, null, 0.006])
  expect(option.series[1].connectNulls).toBe(false)
  expect(option.yAxis.axisLabel.formatter(0.01)).toBe('1.0%')
  expect(host.textContent).toContain('桶内均值, 左轴 %')
})

it('无法比较的轮动强度保留为空，不绘制虚假零值和切换标注', async () => {
  const data = rotation([[null, 0.01, 0.02], [null, 0.02, 0.01]])
  data.timeline = data.timeline.map((point, index) => ({
    ...point, rotation_available: index === 1, rotation: index === 1 ? 0.4 : 0,
  }))
  await render(data)
  const option = optionFor('切换强度')
  expect(option.series[0].data).toEqual([null, 0.4, null])
  expect(option.series[0].connectNulls).toBe(false)
  expect(host.textContent).toContain('切换强度 —（分钟数据不足）')
  const tooltip = option.tooltip!.formatter([{ axisValue: '09:40' }])
  expect(tooltip).toContain('切换强度 —（分钟数据不足）')
  expect(tooltip).not.toContain('切换强度 0.00')
})

it('完整分钟历史无缺口提示，兼容未返回可用性字段的旧响应', async () => {
  await render(rotation([[0.01, 0.02, 0.03]]))
  expect(host.textContent).not.toContain('分钟历史缺口')
  expect(host.textContent).not.toContain('请补采')
  expect(host.textContent).toContain('切换强度 0.00')
  expect(optionFor('切换强度').series[0].data).toEqual([0, 0, 0])
  expect(optionFor('计算机').series[0].data).toEqual([0.01, 0.02, 0.03])
})
