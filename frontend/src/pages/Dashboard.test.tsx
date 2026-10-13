// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, it, vi } from 'vitest'
import { Dashboard } from './Dashboard'

const mocks = vi.hoisted(() => ({ overview: vi.fn(), refreshCache: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { overviewMarket: mocks.overview, refreshCache: mocks.refreshCache } }))
vi.mock('@/lib/useSharedQueries', () => ({ useDataStatus: () => ({ data: {} }), useCapabilities: () => ({ data: {} }) }))
vi.mock('@/components/dashboard/useDashboardLayout', () => ({ useDashboardLayout: () => ({ items: [], setItems: vi.fn() }) }))
vi.mock('@/components/dashboard/registry', () => ({ DEFAULT_LAYOUT: [], widgetDef: vi.fn() }))
vi.mock('@/components/dashboard/DashboardGrid', () => ({ DashboardGrid: ({ ctx }: { ctx: { score: number } }) => <div>市场分数 {ctx.score}</div> }))
vi.mock('@/components/dashboard/AddWidgetPanel', () => ({ AddWidgetPanel: () => null }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
vi.mock('@/components/DimensionMembersDialog', () => ({ DimensionMembersDialog: () => null }))

it('reloads the saved overview without rebuilding shared backend data', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.refreshCache.mockReset().mockResolvedValue({})
  mocks.overview.mockReset().mockResolvedValue({ emotion: { score: 40, label: '震荡' }, limit: {}, quote_status: {} })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const host = document.createElement('div'); const root = createRoot(host)
  async function settle() {
    for (let i = 0; i < 5; i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  }
  try {
    await act(async () => root.render(<QueryClientProvider client={qc}><Dashboard /></QueryClientProvider>)); await settle()
    expect(host.textContent).toContain('市场分数 40')
    mocks.overview.mockResolvedValue({ emotion: { score: 60, label: '偏强' }, limit: {}, quote_status: {} })
    const reload = [...host.querySelectorAll('button')].find(button => button.textContent === '重载')!
    await act(async () => reload.click()); await settle()
    expect(host.textContent).toContain('市场分数 60')
    expect(mocks.refreshCache).not.toHaveBeenCalled()
  } finally { await act(async () => root.unmount()); qc.clear() }
})
