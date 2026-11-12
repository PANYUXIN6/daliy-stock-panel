// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useScreenerRunStatus } from './useScreenerRunStatus'
import { QK } from './queryKeys'
import type { ScreenerTaskStatus } from './api'

const mocks = vi.hoisted(() => ({ summary: vi.fn(), submit: vi.fn(), cancel: vi.fn() }))
vi.mock('./api', () => ({ api: { screenerTasks: mocks.summary, screenerSubmitTask: mocks.submit, screenerCancelRun: mocks.cancel } }))
const blank = { as_of: '2026-11-11', results: {}, tasks: {}, today_ever_counts: {}, updated_at: 0 }
const task = (sid: string, phase: ScreenerTaskStatus['phase'] = 'waiting'): ScreenerTaskStatus => ({
  run_id: `run-${sid}`, metadata: { strategy_id: sid, asset_type: 'stock', timeframe: '1d', as_of: '2026-11-11' },
  phase, pending: [sid], results: {}, errors: {}, error: null, done: false, started_at_ms: 1,
})
let root: Root
let host: HTMLDivElement
let qc: QueryClient
let current: ReturnType<typeof useScreenerRunStatus>
function Harness({ asset = 'stock' }: { asset?: 'stock' | 'etf' }) {
  current = useScreenerRunStatus(asset, '2026-11-11')
  return null
}
async function settle() { await act(async () => { await new Promise(done => setTimeout(done, 15)) }) }
async function render(asset: 'stock' | 'etf' = 'stock') {
  await act(async () => root.render(<QueryClientProvider client={qc}><Harness asset={asset} /></QueryClientProvider>))
  await settle()
}
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  mocks.summary.mockResolvedValue(blank)
  mocks.submit.mockResolvedValue({ task: null, notice: null })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(async () => { await act(async () => root.unmount()); qc.clear(); host.remove() })

it('加载与切换资产只读摘要，不自动提交', async () => {
  await render(); await render('etf'); await render()
  expect(mocks.submit).not.toHaveBeenCalled()
  expect(mocks.summary).toHaveBeenCalledWith('etf', '2026-11-11')
})

it('不同策略独立提交，同一策略首返前和运行中重复点击均复用', async () => {
  const statuses = { a: task('a'), combo: task('combo', 'queued') }
  let resolve!: (value: unknown) => void
  mocks.submit.mockImplementation((sid: string) => sid === 'a' ? new Promise(done => { resolve = done }) : Promise.resolve({ task: statuses.combo, notice: null }))
  await render()
  await act(async () => { void current.submit('a'); void current.submit('a'); void current.submit('combo') })
  expect(mocks.submit.mock.calls.map(args => args[0])).toEqual(['a', 'combo'])
  mocks.summary.mockResolvedValue({ ...blank, tasks: statuses })
  await act(async () => resolve({ task: statuses.a, notice: null }))
  await settle()
  expect(new Set(current.pendingIds)).toEqual(new Set(['a', 'combo']))
  await act(async () => { void current.submit('a'); void current.submit('combo') })
  expect(mocks.submit).toHaveBeenCalledTimes(2)
})

it('重新进入页面从批量查询恢复所有在途任务和独立终态', async () => {
  mocks.summary.mockResolvedValue({ ...blank, tasks: { a: task('a'), b: task('b') } })
  await render()
  expect(current.pendingIds).toEqual(['a', 'b'])
  await act(async () => root.render(null))
  qc.clear()
  mocks.summary.mockResolvedValue({ ...blank, tasks: { a: { ...task('a', 'done'), done: true, pending: [], error: '计算失败' }, b: task('b') } })
  await render()
  expect(current.pendingIds).toEqual(['b'])
  expect(current.errors.a).toBe('计算失败')
  expect(mocks.submit).not.toHaveBeenCalled()
})

it('取消单个任务不会影响另一个策略', async () => {
  mocks.summary.mockResolvedValue({ ...blank, tasks: { a: task('a'), b: task('b') } })
  mocks.cancel.mockResolvedValue({ ...task('a', 'cancelled'), done: true, pending: [] })
  await render()
  await act(async () => current.cancel('a'))
  await settle()
  expect(current.pendingIds).toEqual(['b'])
  expect(mocks.cancel).toHaveBeenCalledWith('run-a')
})

it('提交响应丢失后只查询恢复，状态查询失败也不自动重提', async () => {
  await render()
  mocks.submit.mockRejectedValue(new Error('网络超时'))
  mocks.summary.mockResolvedValue({ ...blank, tasks: { a: task('a') } })
  await act(async () => current.submit('a'))
  await settle()
  expect(current.pendingIds).toEqual(['a'])
  mocks.summary.mockRejectedValue(new Error('暂时断网'))
  await act(async () => qc.invalidateQueries({ queryKey: QK.screenerTasksRoot }))
  await settle()
  expect(current.pendingIds).toEqual(['a'])
  expect(current.connectionNotice).toContain('不会因此取消')
  expect(mocks.submit).toHaveBeenCalledTimes(1)
})

it('切换资产期间的迟发响应不污染当前标签，返回原资产可恢复', async () => {
  let resolve!: (value: unknown) => void
  mocks.submit.mockReturnValue(new Promise(done => { resolve = done }))
  await render()
  await act(async () => { void current.submit('a') })
  await render('etf')
  mocks.summary.mockImplementation(async (asset: string) => asset === 'stock' ? { ...blank, tasks: { a: task('a') } } : blank)
  await act(async () => resolve({ task: task('a'), notice: null }))
  await settle()
  expect(current.pendingIds).toEqual([])
  await render()
  expect(current.pendingIds).toEqual(['a'])
  expect(mocks.submit).toHaveBeenCalledTimes(1)
})
