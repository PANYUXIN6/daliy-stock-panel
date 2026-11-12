// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest'
import { api } from './api'

const { sessionFetch } = vi.hoisted(() => ({ sessionFetch: vi.fn() }))
vi.mock('./sessionRuntime', () => ({ sessionFetch }))
vi.mock('./aiGeneration', () => ({ fetchAiCredits: vi.fn(), runAiGeneration: vi.fn() }))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

beforeEach(() => sessionFetch.mockReset())

it('日线请求明确提交后台任务，并将202响应作为任务受理结果返回', async () => {
  const accepted = { run_id: 'accepted', as_of: '2026-11-11', phase: 'queued', pending: ['composite'], results: {}, complete: false }
  sessionFetch.mockResolvedValue(new Response(JSON.stringify(accepted), { status: 202 }))
  expect(await api.screenerRunAll('2026-11-11', ['composite'])).toEqual(accepted)
  const [path, init] = sessionFetch.mock.calls[0]
  expect(path).toBe('/api/screener/run_all')
  expect(JSON.parse(init.body)).toMatchObject({
    background: true, summary_only: true, timeframe: '1d', strategy_ids: ['composite'],
  })
  expect(sessionFetch).toHaveBeenCalledTimes(1)
})

it('单策略点击通过短请求提交，并接受缓存复用或202任务', async () => {
  const accepted = { task: { run_id: 'combo-task' }, notice: null }
  sessionFetch.mockResolvedValueOnce(new Response(JSON.stringify(accepted), { status: 202 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ task: null, notice: null }), { status: 200 }))
  expect(await api.screenerSubmitTask('combo', 'stock', '2026-11-11')).toEqual(accepted)
  expect(sessionFetch.mock.calls[0][0]).toBe('/api/screener/tasks/combo')
  expect(JSON.parse(sessionFetch.mock.calls[0][1].body)).toEqual({ asset_type: 'stock', as_of: '2026-11-11' })
  expect((await api.screenerSubmitTask('combo', 'stock')).task).toBeNull()
})
