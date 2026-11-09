// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { PipelineJob } from '@/lib/api'
import { ActiveJobCard, STAGE_LABELS } from './ActiveJobCard'
import { HistoryRow } from './SectionTitle'

let host: HTMLDivElement
let root: ReturnType<typeof createRoot>
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-21T08:01:05Z'))
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.useRealTimers()
})
function job(overrides: Partial<PipelineJob> = {}): PipelineJob {
  return { id: 'job-1', status: 'running', stage: 'compute_enriched', progress: 70, stage_pct: 33, started_at: '2026-10-21T08:00:00Z', finished_at: null, duration_s: null, result: null, error: null, log: [], ...overrides }
}
async function render(value: PipelineJob, onDismiss?: () => void) {
  await act(async () => root.render(<ActiveJobCard job={value} onDismiss={onDismiss} />))
}

it('独立显示并行子任务状态、批次消息和耗时，不把预取成功当整个任务完成', async () => {
  await render(job({ tasks: {
    compute_enriched: { label: '计算技术指标', status: 'running', progress: 33, started_at: '2026-10-21T08:00:00Z', last_progress_at: '2026-10-21T08:01:00Z', message: '指标批次 1/3' },
    prefetch_index: { label: 'prefetch_index', status: 'succeeded', progress: 100, duration_s: 2.4 },
    prefetch_industry: { label: '预取行业分类', status: 'pending', progress: 0 },
  }, last_progress_at: '2026-10-21T08:01:00Z' }))
  const rows = [...host.querySelectorAll('[aria-label="各项任务进度"] li')]
  expect(rows).toHaveLength(3)
  expect(rows[0].textContent).toContain('运行中 33%')
  expect(rows[0].textContent).toContain('指标批次 1/3')
  expect(rows[0].textContent).toContain('已用 1m 5s')
  expect(rows[0].textContent).toContain('更新于')
  expect(rows[1].textContent).toContain('完成')
  expect(rows[1].textContent).toContain('预取核心指数')
  expect(host.textContent).not.toContain('prefetch_index')
  expect(rows[1].textContent).toContain('耗时 2s')
  expect(rows[2].textContent).toContain('等待中')
  const help = host.querySelector<HTMLButtonElement>('button[aria-label="总体进度口径"]')!
  expect(host.textContent).not.toContain('总体进度按阶段推进')
  await act(async () => help.click())
  expect(document.getElementById(help.getAttribute('aria-describedby')!)?.textContent).toContain('总体进度按阶段推进，不代表剩余时间')
  expect(host.textContent).toContain('5 秒前')
  await act(async () => vi.advanceTimersByTime(2000))
  expect(host.textContent).toContain('7 秒前')
})

it('失败和取消保持不同语义，错误及子任务错误可见，结束后可主动关闭', async () => {
  const dismiss = vi.fn()
  await render(job({ status: 'failed', error: '当日日线不齐', tasks: { sync_daily: { status: 'failed', error: '缺少 3 只股票' } } }), dismiss)
  expect(host.textContent).toContain('当日日线不齐')
  expect(host.textContent).toContain('缺少 3 只股票')
  expect(host.textContent).not.toContain('已取消')
  await render(job({ status: 'failed', error: '用户手动取消', outcome: 'cancelled' }), dismiss)
  expect(host.textContent).toContain('已取消')
  await act(async () => (host.querySelector('[aria-label="关闭任务详情"]') as HTMLButtonElement).click())
  expect(dismiss).toHaveBeenCalledOnce()
})

it('兼容没有子任务的历史记录并展示阶段耗时', async () => {
  await render(job({ status: 'succeeded', duration_s: 12, stage_timings: { sync_index: 4, compute_regime: 8 }, result: { minute_rows: 0, skipped_stages: ['sync_minute'] } }))
  expect(host.textContent).toContain('同步核心指数与 ETF · 4s')
  expect(host.textContent).toContain('计算市场环境与情绪周期 · 8s')
  expect(host.textContent).toContain('跳过')
})

it('新增管道阶段均有中文名称', () => {
  for (const stage of ['sync_index', 'verify_postmarket', 'sync_classifications', 'compute_regime', 'compute_mainline', 'paper_settle', 'depth_finalize', 'sync_boards', 'publish_review']) {
    expect(STAGE_LABELS[stage]).toMatch(/[\u4e00-\u9fff]/)
  }
})

it.each([
  [true, true, 500, 500, '情绪雷达六项历史已齐备'],
  [true, false, 256, 493, '各项有效 256–493 / 500 日'],
  [true, false, 500, 500, '历史样本已齐，当前部分指标仍不可计算'],
  [false, false, 256, 493, '该日尚未确认正式收盘数据'],
] as const)('完成后明确展示雷达就绪或未就绪原因 %s/%s', async (complete, ready, min, max, message) => {
  await render(job({ status: 'succeeded', result: { analysis: {
    as_of: '2026-11-04', is_complete_day: complete, radar_ready: ready,
    radar_history_min: min, radar_history_max: max,
  } } }))
  expect(host.textContent).toContain('行情与关联分析已更新 · 2026-11-04')
  expect(host.textContent).toContain(message)
})

it('历史记录以取消文案和中性图标展示用户取消', async () => {
  const value = job({ status: 'failed', outcome: 'cancelled', error: '用户手动取消' })
  await act(async () => root.render(<HistoryRow job={value} onClick={() => {}} />))
  expect(host.textContent).toContain('已取消：用户手动取消')
  expect(host.querySelector('svg')?.getAttribute('class')).toContain('text-muted')
  expect(host.textContent).not.toContain('数据同步失败')
})
