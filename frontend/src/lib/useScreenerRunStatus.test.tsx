// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useScreenerRunStatus } from './useScreenerRunStatus'
import { QK } from './queryKeys'
import type { ScreenerRunAllSummary, ScreenerRunStatus } from './api'

const status = vi.hoisted(() => vi.fn())
vi.mock('./api', () => ({ api: { screenerRunStatus: status } }))
let host: HTMLDivElement
let root: Root
let qc: QueryClient
let control: ReturnType<typeof useScreenerRunStatus>
const date = '2026-10-16'
const initial: ScreenerRunAllSummary = { run_id: 'run-1', as_of: date, results: {}, pending: ['fast', 'slow'], started_at: 10000 }
const running: ScreenerRunStatus = { run_id: 'run-1', results: {}, pending: ['fast', 'slow'], errors: {}, error: null, done: false, started_at_ms: 10000 }

function Harness({ scope }: { scope: string }) {
  control = useScreenerRunStatus(scope, date)
  return <>{control.pendingIds.join(',')}|{Object.entries(control.errors).map(([id, reason]) => <p role="alert" key={id}>{id}: {reason}</p>)}{control.notice}</>
}
async function settle() {
  for (let i = 0; i < 4; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function render(scope = `stock|${date}`) {
  await act(async () => root.render(<QueryClientProvider client={qc}><Harness scope={scope} /></QueryClientProvider>))
  await settle()
}
async function start(data = initial) {
  let ticket!: ReturnType<typeof control.begin>
  await act(async () => { ticket = control.begin(['fast', 'slow']) })
  await act(async () => { control.accept(ticket, data) })
  await settle()
  return ticket
}
async function publish(data: ScreenerRunStatus) {
  await act(async () => { qc.setQueryData(QK.screenerRunStatus(data.run_id), data) })
  await settle()
}
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  status.mockReset().mockResolvedValue(running)
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  qc.clear()
  host.remove()
})

it('迟发单策略失败停止对应 spinner，保留其他后台任务并只重试失败项', async () => {
  await render()
  await start()
  await publish({ ...running, pending: ['fast'], errors: { slow: '利润表不可用' } })
  expect(control.pendingIds).toEqual(['fast'])
  expect(control.running).toBe(true)
  expect(control.retryIds).toEqual(['slow'])
  expect(host.textContent).toContain('slow: 利润表不可用')
  await publish({ ...running, pending: [], errors: { slow: '利润表不可用' }, results: { fast: { total: 3, as_of: date } }, done: true })
  expect(control.running).toBe(false)
  expect(control.pendingIds).toEqual([])
  expect(host.querySelectorAll('[role="alert"]')).toHaveLength(1)
})

it('部分成功结束时刷新缓存，重复终态不重复触发反馈', async () => {
  await render()
  await start()
  const invalidate = vi.spyOn(qc, 'invalidateQueries')
  const final = { ...running, pending: [], errors: { slow: '计算失败' }, results: { fast: { total: 2, as_of: date } }, done: true }
  await publish(final)
  expect(invalidate).toHaveBeenCalledWith({ queryKey: QK.screenerCachedRoot })
  invalidate.mockClear()
  await publish({ ...final })
  expect(invalidate).not.toHaveBeenCalled()
  expect(host.querySelectorAll('[role="alert"]')).toHaveLength(1)
})

it('首返错误无需轮询即显示，重试开始清除旧错误', async () => {
  await render()
  await start({ as_of: date, results: { fast: { total: 2, as_of: date } }, pending: [], errors: { slow: '参数不合法' }, complete: true })
  expect(control.errors).toEqual({ slow: '参数不合法' })
  expect(control.running).toBe(false)
  expect(status).not.toHaveBeenCalled()
  await act(async () => { control.begin(['slow']) })
  expect(control.errors).toEqual({})
})

it.each(['etf|2026-10-16', 'stock|2026-10-15'])('切换到%s隔离任务，切回恢复轮询和迟发错误', async otherScope => {
  await render()
  await start()
  await render(otherScope)
  expect(control.pendingIds).toEqual([])
  expect(control.running).toBe(false)
  const final = { ...running, done: true, pending: [], errors: { slow: '迟发错误' } }
  status.mockResolvedValue(final)
  const reads = status.mock.calls.length
  await render()
  expect(status.mock.calls.length).toBeGreaterThan(reads)
  expect(status).toHaveBeenLastCalledWith('run-1')
  expect(control.errors).toEqual({ slow: '迟发错误' })
  expect(control.running).toBe(false)
  await render(otherScope)
  expect(control.errors).toEqual({})
  await render()
  expect(control.errors).toEqual({ slow: '迟发错误' })
})

it('首返期间切换视图仍保存原任务，返回后恢复且不污染另一资产', async () => {
  await render()
  let ticket!: ReturnType<typeof control.begin>
  await act(async () => { ticket = control.begin(['fast', 'slow']) })
  await render('etf|2026-10-16')
  await act(async () => { expect(control.accept(ticket, initial)).toEqual({ scope: `stock|${date}`, visible: false }) })
  expect(control.pendingIds).toEqual([])
  expect(status).not.toHaveBeenCalled()
  await render()
  expect(control.pendingIds).toEqual(['fast', 'slow'])
  expect(status).toHaveBeenCalledWith('run-1')
})

it('不同日期的请求各自保留，同日期新任务才使旧响应失效', async () => {
  await render()
  const old = await start()
  await render('stock|2026-10-15')
  let historical!: ReturnType<typeof control.begin>
  await act(async () => { historical = control.begin(['historical']) })
  await act(async () => { control.reject(historical, new Error('历史任务失败')) })
  expect(control.error).toBe('历史任务失败')
  await render()
  expect(control.pendingIds).toEqual(['fast', 'slow'])
  await act(async () => { control.begin(['new']) })
  await act(async () => { expect(control.accept(old, initial)).toBeNull() })
  await publish({ ...running, done: true, pending: [], errors: { slow: '旧错误' } })
  expect(control.pendingIds).toEqual([])
  expect(control.errors).toEqual({})
  await render('stock|2026-10-15')
  expect(control.error).toBe('历史任务失败')
})

it('临时网络故障保留运行状态，404仅报告无法确认，不称为任务取消', async () => {
  status.mockRejectedValueOnce(new Error('network offline'))
  await render()
  await start()
  expect(control.running).toBe(true)
  expect(control.pendingIds).toEqual(['fast', 'slow'])
  expect(control.errors).toEqual({})
  expect(control.notice).toContain('正在重试')
  status.mockRejectedValueOnce(Object.assign(new Error('gone'), { status: 404 }))
  await act(async () => { await qc.refetchQueries({ queryKey: QK.screenerRunStatus('run-1') }) })
  await settle()
  expect(control.running).toBe(false)
  expect(control.notice).toContain('无法确认')
  expect(control.retryIds).toEqual(['fast', 'slow'])
})

it('旧后端仍按当前日期与本轮时间戳消除已完成任务', async () => {
  await render()
  await start({ ...initial, run_id: undefined })
  await act(async () => control.reconcileSummary({ as_of: date, updated_at: null, today_ever_counts: {}, results: {
    fast: { total: 2, as_of: date, computed_at: 10000 }, slow: { total: 1, as_of: date, computed_at: 1 },
  } }))
  expect(control.pendingIds).toEqual(['slow'])
  expect(status).not.toHaveBeenCalled()
})

it('首返确认默认日期时迁移同一任务，继续跟踪终态', async () => {
  await render('stock|')
  let ticket!: ReturnType<typeof control.begin>
  await act(async () => { ticket = control.begin(['slow']) })
  await act(async () => {
    expect(control.accept(ticket, initial, `stock|${date}`)).toEqual({ scope: `stock|${date}`, visible: true })
    root.render(<QueryClientProvider client={qc}><Harness scope={`stock|${date}`} /></QueryClientProvider>)
  })
  await settle()
  expect(control.running).toBe(true)
  await publish({ ...running, done: true, pending: [], errors: { slow: '迟发错误' } })
  expect(control.errors).toEqual({ slow: '迟发错误' })
})
