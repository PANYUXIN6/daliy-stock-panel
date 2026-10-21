// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { PipelineJobSummary } from '@/lib/api'
import { DataSyncIndicator } from './DataSyncIndicator'

let host: HTMLDivElement
let root: ReturnType<typeof createRoot>
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.useRealTimers()
})

function job(id: string, status: PipelineJobSummary['status'], error: string | null = null): PipelineJobSummary {
  return { id, status, error, stage: 'done', progress: 100, stage_pct: 0, started_at: null, finished_at: null, duration_s: 1, result: null }
}
async function render(activeId: string | null, jobs: PipelineJobSummary[]) {
  await act(async () => root.render(<DataSyncIndicator activeId={activeId} jobs={jobs} />))
}
const status = () => host.querySelector('[role="status"]')?.getAttribute('aria-label')

it('首次进入不把历史成功任务作为刚完成提示', async () => {
  await render(null, [job('old', 'succeeded')])
  expect(status()).toBeUndefined()
})

it('只在观察到的任务真实成功后显示完成，并自动收起', async () => {
  await render('current', [job('current', 'running')])
  expect(status()).toBe('数据同步中')
  await render(null, [job('current', 'succeeded')])
  expect(status()).toBe('数据同步完成')
  await act(async () => vi.advanceTimersByTime(5000))
  expect(status()).toBeUndefined()
})

it('失败与用户取消分别显示，不冒充成功', async () => {
  await render('failed', [job('failed', 'running')])
  await render(null, [job('failed', 'failed', '正式日线尚未齐全')])
  expect(status()).toBe('数据同步失败')
  expect(host.querySelector('[role="status"]')?.getAttribute('title')).toContain('正式日线尚未齐全')
  await render('cancelled', [job('cancelled', 'running')])
  await render(null, [job('cancelled', 'failed', '用户手动取消')])
  expect(status()).toBe('数据同步已取消')
})

it('列表暂时缺失已结束任务时等待真实结果，不采用别的任务的成功状态', async () => {
  await render('current', [job('current', 'running')])
  await render(null, [job('other', 'succeeded')])
  expect(status()).toBeUndefined()
  await render(null, [job('current', 'failed', '上游取数失败'), job('other', 'succeeded')])
  expect(status()).toBe('数据同步失败')
})

it('超时自动取消作为失败，新的活跃任务覆盖上一条结束提示', async () => {
  await render('timeout', [job('timeout', 'running')])
  await render(null, [job('timeout', 'failed', '进度停滞超时自动取消')])
  expect(status()).toBe('数据同步失败')
  await render('next', [job('next', 'running')])
  expect(status()).toBe('数据同步中')
})
