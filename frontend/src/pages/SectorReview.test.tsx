// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { SectorReview } from './SectorReview'
import { SectorReviewDetails } from '@/components/sector-review/SectorReviewDetails'
import { QK, dataUpdateQueryPrefixes, SSE_INVALIDATE_PREFIXES } from '@/lib/queryKeys'
import { ApiError, type SectorReviewAvailability, type SectorReviewResult, type SectorReviewRow } from '@/lib/api'

const mocks = vi.hoisted(() => ({ availability: vi.fn(), review: vi.fn(), members: vi.fn(), preview: vi.fn() }))
vi.mock('@/lib/api', async importOriginal => {
  const original = await importOriginal<typeof import('@/lib/api')>()
  return { ...original, api: { sectorReviewAvailability: mocks.availability, sectorReview: mocks.review, sectorReviewMembers: mocks.members } }
})
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: (props: unknown) => { mocks.preview(props); return null } }))
const down = { wins: 3, comparable_days: 4, benchmark_down_days: 4, known_down_days: 4, ratio: .75, mean_excess: .01, complete: true, small_sample: true }
const row: SectorReviewRow = {
  sector_id: 'a', name: '测试行业', path: '一级-测试行业', sector_return: .02, benchmark_return: -.01, relative_return: .0303,
  classification: 'countertrend_up', down_days: down, return_days: 20, required_days: 20,
  original_members: 6, formed_members: 6, exclusions: {}, eligible_for_comparison: true, data_complete: false,
  missing_reasons: {}, member_up_ratio: .8, member_median_return: .01, member_return_count: 6, core_members: [],
  latest: { date: '2026-10-19', status: 'partial', day_return: .02, benchmark_return: -.01, breadth_up: .8 },
  observations: [{ date: '2026-10-19', status: 'partial', day_return: .02, benchmark_return: -.01, strength: true, daily_resonance_candidate: null }],
  candidate: true, confirmed_first_strength_date: '2026-10-19', earliest_observed_strength_date: '2026-10-19', first_strength_offset: 0, strength_streak: 1, streak_complete: true,
}
const data: SectorReviewResult = {
  status: 'partial', context: { mode: 'event', kind: 'industry', dimension_id: 'd', level: 1, benchmark: '000001.SH', as_of: '2026-10-19', event_date: '2026-10-19', view_as_of: '2026-10-19', window: null, formation_date: '2026-10-16' },
  rules_version: 'sector-review-v1', snapshot_id: 'v1', membership_basis: 'current_snapshot_backcast', calculation_basis: 'recomputed', warnings: [],
  coverage: { total_sectors: 2, evaluated_sectors: 1, complete_sectors: 0 }, rows: [row, { ...row, sector_id: 'b', name: '待评估板块', path: '待评估板块', candidate: null, relative_return: null, sector_return: null }],
}
const info: SectorReviewAvailability = {
  status: 'partial', default_date: '2026-10-19', latest_local_date: '2026-10-19',
  calendar: { start: '2026-01-01', end: '2026-10-23', days: ['2026-10-16', '2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22', '2026-10-23'] },
  dimensions: [{ dimension_id: 'd', config_id: 'test', field: 'industry', label: '行业来源', kind: 'industry', levels: [1, 2, 3], mode: 'snapshot' }],
  benchmarks: [{ symbol: '000001.SH', name: '上证指数' }], etf: { status: 'unconfigured', reason: '未配置' }, warnings: [],
}
const cleanups: (() => Promise<void>)[] = []
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  mocks.availability.mockResolvedValue(info)
  mocks.review.mockResolvedValue(data)
  mocks.members.mockResolvedValue({ snapshot_id: 'v1', sector_id: 'a', page: 1, page_size: 30, total: 1, rows: [{ symbol: '600000.SH', name: '历史成员', formation_eligible: true, exclusion_reason: null, is_core: true, window_return: .01, observations: [{ date: '2026-10-19', price: 10, day_return: null, amount: null, amount_ratio: null, reason: 'missing_quote' }] }] })
})
afterEach(async () => { for (const cleanup of cleanups.splice(0)) await cleanup() })
async function settle() { for (let i = 0; i < 5; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) }) }
function Location() { return <output data-location>{useLocation().search}</output> }
async function mount(element: React.ReactNode = <SectorReview />, entry = '/sector-review') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const host = document.createElement('div'); document.body.append(host)
  const root = createRoot(host)
  cleanups.push(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
  await act(async () => root.render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[entry]}>{element}<Location /></MemoryRouter></QueryClientProvider>))
  await settle(); return { host, client, root }
}
async function click(host: HTMLElement, text: string) {
  const button = [...host.querySelectorAll('button')].find(node => node.textContent === text || node.textContent?.includes(text))!
  expect(button).toBeTruthy(); await act(async () => button.click()); await settle()
}
async function select(host: HTMLElement, label: string, value: string) {
  const input = host.querySelector<HTMLSelectElement>(`select[aria-label="${label}"]`)!
  await act(async () => { input.value = value; input.dispatchEvent(new Event('change', { bubbles: true })) }); await settle()
}

it('retains price candidates with missing auxiliary evidence, and filters three-valued candidates', async () => {
  const { host } = await mount()
  expect(host.textContent).toContain('当前分类名册回看历史行情')
  expect(host.textContent).toContain('测试行业')
  expect(host.textContent).not.toContain('待评估板块')
  expect(host.textContent).toContain('数据不足')
  expect(host.textContent).toContain('3.03%')
  await select(host, '结果筛选', 'unknown')
  expect(host.textContent).toContain('待评估板块')
  expect(host.textContent).toContain('—')
  await select(host, '结果筛选', 'false')
  expect(host.textContent).toContain('当前筛选条件下没有板块')
})

it('restores URL context, switches windows, keeps context in the URL, and uses conditional denominator', async () => {
  const { host } = await mount(<SectorReview />, '/sector-review?view=countertrend&kind=industry&dimension_id=d&as_of=2026-10-19&window=5')
  expect(mocks.review).toHaveBeenLastCalledWith('countertrend', expect.objectContaining({ window: 5, as_of: '2026-10-19' }), expect.any(AbortSignal))
  expect(host.textContent).toContain('3/4 · 共 4 天')
  await select(host, '逆势窗口', '60')
  expect(mocks.review).toHaveBeenLastCalledWith('countertrend', expect.objectContaining({ window: 60 }), expect.any(AbortSignal))
  expect(host.querySelector('[data-location]')?.textContent).toContain('window=60')
  await click(host, '事件共振跟踪')
  expect(host.querySelector('input[aria-label="事件日T"]')).toBeTruthy()
})

it('shows recoverable errors, data unavailable and empty candidate results separately', async () => {
  mocks.review.mockRejectedValue(new Error('读取失败'))
  const error = await mount()
  expect(error.host.textContent).toContain('板块复盘加载失败')
  expect(error.host.querySelector('input[aria-label="事件日T"]')).toBeTruthy()
  mocks.review.mockResolvedValue({ ...data, status: 'unavailable', rows: [] })
  await click(error.host, '重试')
  expect(error.host.textContent).toContain('本地正式数据不足')
  mocks.review.mockResolvedValue({ ...data, status: 'ready', rows: [{ ...row, candidate: false }] })
  const empty = await mount()
  expect(empty.host.textContent).toContain('该日期没有满足当前条件')
})

it('reads historical members lazily, keeps null values, and labels current preview', async () => {
  const { host } = await mount(<SectorReviewDetails row={row} mode="event" params={{ kind: 'industry', dimension_id: 'd', level: 1, benchmark: '000001.SH', event_date: '2026-10-19' }} snapshotId="v1" onRefresh={() => {}} />)
  expect(mocks.members).not.toHaveBeenCalled()
  await click(host, '查看历史成员证据')
  expect(host.textContent).toContain('历史成员')
  expect(host.textContent).toContain('前复权收盘 10.000 · —')
  expect(host.textContent).toContain('未合并当前报价')
  await click(host, '查看当前行情')
  expect(mocks.preview.mock.lastCall?.[0]).toMatchObject({ symbol: '600000.SH' })
})

it('keeps the summary and offers refresh on member version conflicts', async () => {
  const refresh = vi.fn()
  mocks.members.mockRejectedValue(new ApiError('数据已更新', 409))
  const { host } = await mount(<SectorReviewDetails row={row} mode="event" params={{ kind: 'industry', dimension_id: 'd', level: 1, benchmark: '000001.SH' }} snapshotId="v1" onRefresh={refresh} />)
  await click(host, '查看历史成员证据')
  expect(host.textContent).toContain('历史证据')
  expect(host.textContent).toContain('数据已更新，刷新后查看')
  await click(host, '刷新复盘')
  expect(refresh).toHaveBeenCalledOnce()
})

it('isolates old context responses and resets expanded details on context changes', async () => {
  let resolveOld!: (value: SectorReviewResult) => void
  mocks.review.mockImplementation((mode: string) => mode === 'event' ? new Promise<SectorReviewResult>(resolve => { resolveOld = resolve }) : Promise.resolve({ ...data, rows: [{ ...row, name: '当前逆势板块' }] }))
  const { host } = await mount()
  await click(host, '调整逆势')
  expect(host.textContent).toContain('当前逆势板块')
  await act(async () => resolveOld({ ...data, rows: [{ ...row, name: '旧事件板块' }] })); await settle()
  expect(host.textContent).not.toContain('旧事件板块')
})

it('switches source and level together before requesting the new context', async () => {
  mocks.availability.mockResolvedValue({ ...info, dimensions: [...info.dimensions, { ...info.dimensions[0], dimension_id: 'flat', config_id: 'flat', label: '仅一级来源', levels: [1] }] })
  const { host } = await mount(<SectorReview />, '/sector-review?view=event&kind=industry&dimension_id=d&event_date=2026-10-19&level=3')
  expect(mocks.review).toHaveBeenLastCalledWith('event', expect.objectContaining({ dimension_id: 'd', level: 3 }), expect.any(AbortSignal))
  await select(host, '分类来源', 'flat')
  expect(mocks.review).toHaveBeenLastCalledWith('event', expect.objectContaining({ dimension_id: 'flat', level: 1 }), expect.any(AbortSignal))
  expect(mocks.review.mock.calls.filter(([, params]) => params.dimension_id === 'flat').every(([, params]) => params.level === 1)).toBe(true)
  expect(host.querySelector<HTMLSelectElement>('select[aria-label="行业层级"]')?.value).toBe('1')
  expect(host.querySelector('[data-location]')?.textContent).toContain('level=1')
})

it('normalizes an unsupported URL level before the first source request', async () => {
  mocks.availability.mockResolvedValue({ ...info, dimensions: [{ ...info.dimensions[0], levels: [1] }] })
  const { host } = await mount(<SectorReview />, '/sector-review?dimension_id=d&level=3')
  expect(mocks.review).toHaveBeenCalled()
  expect(mocks.review.mock.calls.every(([, params]) => params.level === 1)).toBe(true)
  expect(host.querySelector('[data-location]')?.textContent).toContain('level=1')
})

it('invalidates on daily/classification/calendar/config updates without quote-driven recomputation', () => {
  for (const dataset of ['daily', 'enriched', 'ext_data', 'calendar', 'etf', 'index', 'config']) expect(dataUpdateQueryPrefixes([dataset])).toContain('sector-review')
  expect(SSE_INVALIDATE_PREFIXES).not.toContain('sector-review')
  expect(QK.sectorReview('event', { event_date: '2026-10-19' })).not.toEqual(QK.sectorReview('event', { event_date: '2026-10-20' }))
})
