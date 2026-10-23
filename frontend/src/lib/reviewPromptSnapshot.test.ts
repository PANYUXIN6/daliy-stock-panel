import { afterEach, expect, it, vi } from 'vitest'
import { feedReviewEvent, getReviewState, resetReview, setViewingReport, startReviewGeneration } from './reviewStore'

const snapshot = { source: 'custom' as const, text: '生成时的模板', sha256: 'template-hash' }
vi.mock('@/lib/api', () => ({ api: {
  async *reviewStream() {
    yield { type: 'meta', as_of: '2026-10-22', system_prompt: snapshot }
    yield { type: 'delta', content: '复盘正文' }
    yield { type: 'done', archived: true }
  },
}, friendlyStreamError: (value: string) => value }))
afterEach(resetReview)

it('retains the exact template across manual generation and history reload', async () => {
  await startReviewGeneration('2026-10-22', '')
  expect(getReviewState().meta?.system_prompt).toEqual(snapshot)
  resetReview()
  setViewingReport({ as_of: '2026-10-22', content: '复盘正文', system_prompt: snapshot })
  expect(getReviewState().meta?.system_prompt).toEqual(snapshot)
  setViewingReport({ as_of: '2026-10-21', content: '旧版历史报告' })
  expect(getReviewState().meta?.system_prompt).toBeUndefined()
})

it('keeps the scheduled owner template snapshot from private SSE', () => {
  feedReviewEvent({ type: 'meta', system_prompt: snapshot })
  feedReviewEvent({ type: 'delta', content: '定时复盘' })
  feedReviewEvent({ type: 'done', archived: true })
  expect(getReviewState().meta?.system_prompt).toEqual(snapshot)
  expect(getReviewState().phase).toBe('done')
})
