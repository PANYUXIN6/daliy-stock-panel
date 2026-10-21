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
  const sectors = ['计算机', '银行', '医药', '食品饮料', '家用电器', '电力设备', '有色金属', '机械设备', '传媒', '通信', '汽车', '房地产'].slice(0, matrix.length)
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

function rotationWithCoverage(matrix: (number | null)[][], coverage: NonNullable<SectorRotation['coverage']>): SectorRotation {
  const data = rotation(matrix)
  data.coverage = coverage
  data.sectors = data.series!.sectors.map((name, index) => {
    const pct = matrix[index].at(-1) ?? null
    return {
      name, pct_now: pct, pct_prev: matrix[index][0],
      rank_now: pct == null ? null : index + 1, rank_prev: index + 1,
      rank_change: pct == null ? null : 0, flow: null,
      score: pct == null ? null : 50, n_members: 2, n_members_with_bars: pct == null ? 0 : 2,
    }
  })
  return data
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

function sectorRow(name: string): HTMLElement {
  const nameCell = [...host.querySelectorAll('span[title]')]
    .find(node => node.getAttribute('title')?.startsWith(`${name} · 成分 `))
  if (!nameCell?.parentElement) throw new Error(`找不到榜单行业 ${name}`)
  return nameCell.parentElement
}

it('稀疏分钟数据提示实际缺口行业数，保留空档且单个数据点可见', async () => {
  await render(rotation([[null, null, 0.01], [0.005, null, 0.006], [0.01, 0.02, 0.03]]))
  expect(host.textContent).toContain('当前展示的 2 个行业存在分钟历史缺口')
  expect(host.textContent).toContain('数据 → 同步分钟历史数据')
  expect(host.textContent).toContain('补采 2024-10-18 的分钟数据')
  expect(host.querySelector('a[href="/data"]')).not.toBeNull()
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
  expect(host.textContent).not.toContain('当日已采集')
  expect(host.textContent).not.toContain('覆盖不足')
  expect(host.textContent).toContain('切换强度 0.00')
  expect(optionFor('切换强度').series[0].data).toEqual([0, 0, 0])
  expect(optionFor('计算机').series[0].data).toEqual([0.01, 0.02, 0.03])
})

it('单股产生的新日期明确只有部分行业当日有数据，提示普通分钟历史补采', async () => {
  await render(rotationWithCoverage([[0.01, 0.02, 0.03]], {
    known_sectors: 12, day_sectors: 1, latest_sectors: 1,
  }))
  expect(host.textContent).toContain('当日已采集 1/12 个行业；最新时段更新 1 个')
  expect(host.textContent).toContain('覆盖不足，仅显示已采集数据')
  expect(host.textContent).toContain('数据 → 同步分钟历史数据')
  expect(host.querySelector('a[href="/data"]')).not.toBeNull()
  expect(host.textContent).not.toContain('分钟历史缺口')
  expect(host.textContent).not.toContain('采集完整')
})

it('同日最新桶稀疏时保留十二个行业历史，当前缺行情的行业标记未更新且不沿用旧涨幅', async () => {
  const matrix = [[0.01, 0.02, 0.03], ...Array.from({ length: 11 }, () => [0.01, 0.02, null])]
  await render(rotationWithCoverage(matrix, {
    known_sectors: 12, day_sectors: 12, latest_sectors: 1,
  }))
  expect(host.textContent).toContain('当日已采集 12/12 个行业；最新时段更新 1 个')
  expect(host.textContent).toContain('覆盖不足')
  expect(host.textContent).toContain('数据 → 同步分钟历史数据')
  const staleRow = sectorRow('银行')
  expect(staleRow.textContent).toContain('本时段未更新')
  expect(staleRow.children[1].textContent).toBe('—')
  expect(staleRow.children[2].textContent).toBe('1.00%')
  expect(staleRow.children[3].textContent).toBe('—')
  expect(staleRow.children[4].textContent).toBe('—分')
  expect(sectorRow('房地产').textContent).toContain('本时段未更新')
  expect(sectorRow('计算机').textContent).not.toContain('本时段未更新')
  const option = optionFor('银行')
  expect(option.series).toHaveLength(12)
  expect(option.series[1].data).toEqual([0.01, 0.02, null])
  expect(option.series[1].connectNulls).toBe(false)
})

it('覆盖统计齐全时展示实际数量，不把覆盖数量当作完整性验收', async () => {
  await render(rotationWithCoverage(Array.from({ length: 12 }, () => [0.01, 0.02, 0.03]), {
    known_sectors: 12, day_sectors: 12, latest_sectors: 12,
  }))
  expect(host.textContent).toContain('当日已采集 12/12 个行业；最新时段更新 12 个')
  expect(host.textContent).not.toContain('覆盖不足')
  expect(host.textContent).not.toContain('本时段未更新')
  expect(host.textContent).not.toContain('分钟历史缺口')
  expect(host.querySelector('a[href="/data"]')).toBeNull()
  expect(host.textContent).not.toContain('采集完整')
})

it('最新桶真实零涨幅保持0.00%，不误标为未更新', async () => {
  await render(rotationWithCoverage([[0.01, 0.02, 0]], {
    known_sectors: 1, day_sectors: 1, latest_sectors: 1,
  }))
  const row = sectorRow('计算机')
  expect(row.children[1].textContent).toBe('0.00%')
  expect(row.textContent).not.toContain('本时段未更新')
  expect(optionFor('计算机').series[0].data).toEqual([0.01, 0.02, 0])
})

it('全部行业最新已更新时仍保留早段历史缺口提示', async () => {
  await render(rotationWithCoverage([[0.01, null, 0.03]], {
    known_sectors: 1, day_sectors: 1, latest_sectors: 1,
  }))
  expect(host.textContent).toContain('当日已采集 1/1 个行业；最新时段更新 1 个')
  expect(host.textContent).not.toContain('覆盖不足')
  expect(host.textContent).toContain('当前展示的 1 个行业存在分钟历史缺口')
  expect(host.textContent).toContain('数据 → 同步分钟历史数据')
  expect(optionFor('计算机').series[0].data).toEqual([0.01, null, 0.03])
})
