// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { ExtDataConfig, ExtDataRowsResult } from '@/lib/api'
import type { AnalysisFieldConfig } from '@/components/analysis-shared'
import { IndustryAnalysis } from './IndustryAnalysis'

const fixtures = vi.hoisted(() => ({
  config: {} as AnalysisFieldConfig,
  sources: [] as ExtDataConfig[],
  rows: {} as Record<string, ExtDataRowsResult>,
}))

vi.mock('@/lib/api', () => ({ api: {
  extDataList: async () => ({ items: fixtures.sources }),
  extDataRows: async (id: string) => fixtures.rows[id],
  marketSnapshot: async () => ({ rows: [] }),
} }))
vi.mock('@/lib/storage', () => ({ storage: {
  industryAnalysisConfig: { get: () => fixtures.config, set: vi.fn() },
} }))
vi.mock('@/components/analysis-shared', () => ({
  AnalysisConfigDialog: () => null,
  DimensionHeatmap: () => null,
}))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
vi.mock('@/components/RpsRotationDialog', () => ({ RpsRotationDialog: () => null }))
vi.mock('@/components/SectorRotationCard', () => ({ SectorRotationCard: () => null }))

function source(id: string, label: string, field: string, description: string): ExtDataConfig {
  return {
    id, label, description, mode: 'snapshot', created_at: '', updated_at: '',
    fields: [
      { name: 'symbol', label: '股票代码', dtype: 'string' },
      { name: 'name', label: '股票简称', dtype: 'string' },
      { name: field, label: field, dtype: 'string' },
    ],
  }
}

function savedRows(config: ExtDataConfig, field: string, value: string): ExtDataRowsResult {
  return {
    id: config.id, label: config.label, mode: config.mode, fields: config.fields,
    date: '2026-10-09', total: 1, limit: 12000,
    rows: [{ symbol: '600000.SH', name: '浦发银行', [field]: value }],
  }
}

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  fixtures.config = {}
  const concept = source('ext_gn_ths', '同花顺概念', '所属概念', '同花顺概念分类 (启动仅创建配置, 在概念/行业页手动获取)')
  const industry = source('ext_hy_ths', '同花顺行业', '所属行业', '同花顺行业分类 (启动仅创建配置, 在概念/行业页手动获取)')
  fixtures.sources = [concept, industry]
  fixtures.rows = {
    [concept.id]: savedRows(concept, '所属概念', 'AI视频'),
    [industry.id]: savedRows(industry, '所属行业', '银行'),
  }
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

async function renderPage() {
  await act(async () => root.render(<QueryClientProvider client={client}><IndustryAnalysis /></QueryClientProvider>))
  // React Query batches observer notifications on the next timer tick.
  for (let i = 0; i < 5; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

it('automatically shows industry groups when an earlier concept source has a shared page description', async () => {
  await renderPage()
  expect(host.textContent).toContain('1 个行业')
  expect(host.textContent).toContain('最强行业银行')
})

it('keeps an explicitly selected custom source and dimension field', async () => {
  const custom = source('custom_classifications', '自定义分类', '分类名称', '研究分类')
  fixtures.sources.push(custom)
  fixtures.rows[custom.id] = savedRows(custom, '分类名称', '自定义银行组')
  fixtures.config = { configId: custom.id, dimensionField: '分类名称' }
  await renderPage()
  expect(host.textContent).toContain('最强行业自定义银行组')
})

it('still recognizes a custom source identified only by its description', async () => {
  const custom = source('custom_classifications', '自定义分类', '分类名称', '行业分类数据')
  fixtures.sources = [custom]
  fixtures.rows = { [custom.id]: savedRows(custom, '分类名称', '自定义银行组') }
  await renderPage()
  expect(host.textContent).toContain('1 个行业')
  expect(host.textContent).toContain('最强行业自定义银行组')
})
