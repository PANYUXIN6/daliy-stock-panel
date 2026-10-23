// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { LimitUpLadder } from './LimitUpLadder'
import { StrengthLadder } from '@/components/StrengthLadder'
import { QK, dataUpdateQueryPrefixes, SSE_INVALIDATE_PREFIXES } from '@/lib/queryKeys'
import type { StrengthLadderResult } from '@/lib/api'

const mocks = vi.hoisted(() => ({ strength: vi.fn(), limit: vi.fn(), preview: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { strengthLadder: mocks.strength, limitLadder: mocks.limit } }))
vi.mock('@/lib/useSharedQueries', () => ({ useCapabilities: () => ({ data: {} }), usePreferences: () => ({ data: {} }) }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: (props: unknown) => { mocks.preview(props); return null } }))
vi.mock('@/components/DimensionMembersDialog', () => ({ DimensionMembersDialog: () => null }))
vi.mock('@/components/DatePicker', () => ({ DatePicker: ({ value, onChange }: { value: string; onChange: (value: string) => void }) => <input aria-label="交易日期" type="date" value={value} onChange={event => onChange(event.target.value)} /> }))

const data: StrengthLadderResult = {
  as_of: '2026-10-22', total: 10, eligible: 9, counts: { near_high: 2, near_low: 1 }, warnings: ['一只股票历史不足'],
  tiers: [
    { level: 5, minimum: 0.25, maximum: null, count: 1, stocks: [{ symbol: '600000.SH', name: '新高股票', close: 13, change_pct: 0.02, momentum_20d: 0.3, vol_ratio_5d: 2, status: 'high' }] },
    { level: 4, minimum: 0.15, maximum: 0.25, count: 1, stocks: [{ symbol: '302001.SZ', name: '放量股票', close: 12, change_pct: 0.01, momentum_20d: 0.16, vol_ratio_5d: 1.5, status: 'volume' }] },
    { level: 3, minimum: 0.08, maximum: 0.15, count: 1, stocks: [{ symbol: '689001.SH', name: '动量股票', close: 11, change_pct: -0.01, momentum_20d: 0.09, vol_ratio_5d: 1, status: 'momentum' }] },
  ],
}
const cleanups: (() => Promise<void>)[] = []
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  mocks.strength.mockResolvedValue(data)
  mocks.limit.mockResolvedValue({ as_of: data.as_of, tiers: [], counts: { up: 0, down: 0 } })
})
afterEach(async () => { for (const cleanup of cleanups.splice(0)) await cleanup() })
async function settle() { for (let i = 0; i < 5; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) }) }
async function mount(element: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const host = document.createElement('div'); document.body.append(host)
  const root = createRoot(host)
  cleanups.push(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
  await act(async () => root.render(<QueryClientProvider client={client}>{element}</QueryClientProvider>))
  await settle()
  return { host, client, root }
}
async function click(host: HTMLElement, text: string) {
  const button = [...host.querySelectorAll('button')].find(node => node.textContent?.includes(text))!
  expect(button).toBeTruthy()
  await act(async () => button.click()); await settle()
}
async function select(host: HTMLElement, label: string, value: string) {
  const input = host.querySelector<HTMLSelectElement>(`select[aria-label="${label}"]`)!
  await act(async () => { input.value = value; input.dispatchEvent(new Event('change', { bubbles: true })) })
}

it('opens the A-share trend tab, filters statuses and boards, and navigates the visible stock list', async () => {
  const { host } = await mount(<LimitUpLadder />)
  expect(mocks.strength).not.toHaveBeenCalled()
  await click(host, '趋势强度')
  expect(mocks.strength).toHaveBeenCalledWith(undefined)
  expect(host.textContent).toContain('20日涨幅 25%及以上')
  expect(host.textContent).toContain('一只股票历史不足')
  expect(host.textContent).toContain('可计算 9/10')
  await select(host, '趋势状态', 'volume')
  expect(host.textContent).toContain('放量股票')
  expect(host.textContent).not.toContain('新高股票')
  await click(host, '放量股票')
  expect(mocks.preview.mock.lastCall?.[0]).toMatchObject({ symbol: '302001.SZ', navList: [{ symbol: '302001.SZ' }] })
  await select(host, '趋势状态', 'all')
  await select(host, '趋势板块', '科创板')
  expect(host.textContent).toContain('动量股票')
  expect(host.textContent).not.toContain('放量股票')
  await click(host, '连板 / 连跌')
  expect(host.textContent).toContain('暂无连板数据')
})

it('isolates historical requests and keeps date controls in loading and empty states', async () => {
  let resolveHistory!: (value: StrengthLadderResult) => void
  mocks.strength.mockImplementation((asOf?: string) => asOf ? new Promise<StrengthLadderResult>(resolve => { resolveHistory = resolve }) : Promise.resolve(data))
  const { host, root, client } = await mount(<StrengthLadder asOf="" onDateChange={() => {}} />)
  await act(async () => root.render(<QueryClientProvider client={client}><StrengthLadder asOf="2026-10-21" onDateChange={() => {}} /></QueryClientProvider>))
  expect(host.textContent).not.toContain('新高股票')
  expect(host.querySelector('input[aria-label="交易日期"]')).toBeTruthy()
  expect(mocks.strength).toHaveBeenLastCalledWith('2026-10-21')
  await act(async () => resolveHistory({ ...data, as_of: '2026-10-21', tiers: [] })); await settle()
  expect(host.textContent).toContain('暂无趋势股票')
  expect(host.textContent).toContain('返回最新')
  expect(client.getQueryData(QK.strengthLadder())).toEqual(data)
})

it('shows a recoverable error while keeping date controls', async () => {
  mocks.strength.mockRejectedValue(new Error('unavailable'))
  const { host } = await mount(<StrengthLadder asOf="" onDateChange={() => {}} />)
  expect(host.textContent).toContain('趋势梯队加载失败')
  expect(host.querySelector('input[aria-label="交易日期"]')).toBeTruthy()
  expect(host.textContent).toContain('重试')
})

it('shares the ladder refresh preference and daily publication invalidation without colliding with limit results', () => {
  expect(QK.strengthLadder()[0]).toBe(QK.limitLadder()[0])
  expect(SSE_INVALIDATE_PREFIXES).toContain(QK.strengthLadder()[0])
  expect(dataUpdateQueryPrefixes(['daily'])).toContain(QK.strengthLadder()[0])
  expect(QK.strengthLadder('2026-10-21')).not.toEqual(QK.limitLadder('2026-10-21'))
})
