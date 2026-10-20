// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Review } from './Review'
import { resetReview, setViewingReport } from '@/lib/reviewStore'
import { AuthContext } from '@/lib/auth'
import { creditsFixture, guestUser } from '@/test/aiCredits'
const mocks = vi.hoisted(() => ({ publication: vi.fn(), generate: vi.fn(), save: vi.fn(), credits: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: {
  reviewPublication: mocks.publication,
  dragonTiger: async () => ({ state: 'no_data', stocks: [] }),
  reviewReportsList: async () => ({ reports: [{ id: 'old', as_of: '2026-10-08', content: '历史报告', created_at: '2026-10-08T16:00:00' }] }),
  reviewReportSave: mocks.save,
  aiCredits: mocks.credits,
}, friendlyStreamError: (s: string) => s }))
vi.mock('@/lib/useSharedQueries', () => ({ usePreferences: () => ({ data: {} }) }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
vi.mock('@/lib/reviewStore', async importOriginal => ({ ...(await importOriginal<object>()), startReviewGeneration: mocks.generate }))
beforeEach(() => { vi.clearAllMocks(); resetReview(); mocks.credits.mockResolvedValue(creditsFixture()) })
afterEach(() => { vi.useRealTimers() })
async function settle() {
  for (let i = 0; i < 5; i++) await act(async () => {
    if (vi.isFakeTimers()) await vi.advanceTimersByTimeAsync(1)
    else await new Promise(r => setTimeout(r, 0))
  })
}
it('shows unpublished status while keeping history and manual AI generation available without duplicate archival', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.publication.mockResolvedValue({ as_of: '2026-10-09', published: false, published_at: null, overview: null })
  mocks.generate.mockImplementation((_asOf, _focus, done) => done('新报告', { as_of: '2026-10-09', archived: true }))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const host = document.createElement('div'); const root = createRoot(host)
  try {
    await act(async () => root.render(<AuthContext.Provider value={guestUser}><MemoryRouter><QueryClientProvider client={qc}><Review /></QueryClientProvider></MemoryRouter></AuthContext.Provider>))
    await settle()
    expect(host.textContent).toContain('当日复盘尚未更新')
    expect(host.textContent).toContain('2026-10-08')
    expect(mocks.generate).not.toHaveBeenCalled()
    const button = [...host.querySelectorAll('button')].find(b => b.textContent?.startsWith('生成复盘 ·'))!
    expect(button.disabled).toBe(false)
    await act(async () => button.click()); await settle()
    expect(mocks.generate).toHaveBeenCalledTimes(1)
    expect(mocks.save).not.toHaveBeenCalled()
  } finally { await act(async () => root.unmount()); qc.clear() }
})

it('changes the publication date at Beijing midnight and uses the selected historical report date', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-09T23:59:59+08:00'))
  setViewingReport({ as_of: '2026-10-09', content: '昨日 AI 报告正文' })
  mocks.publication.mockImplementation(async (day: string) => ({
    as_of: day, published: day !== '2026-10-10', published_at: null,
    overview: day === '2026-10-10' ? null : { as_of: day, emotion: { score: 50, label: day === '2026-10-08' ? '历史情绪' : '昨日情绪' } },
  }))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const host = document.createElement('div'); const root = createRoot(host)
  try {
    await act(async () => root.render(<AuthContext.Provider value={guestUser}><MemoryRouter><QueryClientProvider client={qc}><Review /></QueryClientProvider></MemoryRouter></AuthContext.Provider>))
    await settle()
    expect(host.textContent).toContain('昨日情绪')
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) }); await settle()
    expect(mocks.publication).toHaveBeenCalledWith('2026-10-10')
    expect(host.textContent).toContain('当日复盘尚未更新')
    expect(host.textContent).not.toContain('昨日情绪')
    expect(host.textContent).toContain('复盘报告 · 2026-10-09')
    expect(host.textContent).toContain('昨日 AI 报告正文')
    expect(mocks.generate).not.toHaveBeenCalled()
    const historyDate = [...host.querySelectorAll('span')].find(n => n.textContent === '2026-10-08')!
    await act(async () => historyDate.click()); await settle()
    expect(mocks.publication).toHaveBeenCalledWith('2026-10-08')
    expect(host.textContent).toContain('历史情绪')
    expect(host.textContent).toContain('历史复盘 · 2026-10-08')
    expect(host.textContent).not.toContain('当日复盘尚未更新')
  } finally { await act(async () => root.unmount()); qc.clear() }
})
