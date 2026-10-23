import { activateSession } from './sessionRuntime'
import { beforeEach } from 'vitest'
// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, it, vi } from 'vitest'
import { useQuoteStream } from './useQuoteStream'
import { QK } from './queryKeys'
vi.mock('@/components/AlertToast', () => ({ pushAlertToasts: vi.fn() }))
vi.mock('./stockAnalysisStore', () => ({ loadHistory: vi.fn() }))
vi.mock('./aiReportStore', () => ({ loadHistory: vi.fn() }))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
class Stream extends EventTarget {
  static current: Stream
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor() { super(); Stream.current = this }
  close() {}
}
function Probe() { useQuoteStream(false, undefined); return null }
function LiveProbe({ enabled = true, pages }: { enabled?: boolean; pages?: Record<string, boolean> }) {
  useQuoteStream(enabled, pages)
  return null
}
it('refreshes collected extension and environment results across pages without invalidating unrelated data', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('EventSource', Stream)
  const qc = new QueryClient()
  const keys = [['ext-data-rows', 'ext_gn_ths'], ['dimension-members', 'ext_gn_ths'], ['rps-rotation', 12], ['regime-history', 'all'], ['regime-mainline', 'concept'], ['regime-phases', '', ''], ['financials', 'metrics'], ['unrelated']]
  for (const key of keys) qc.setQueryData(key, { value: 1 })
  const host = document.createElement('div'); const root = createRoot(host)
  try {
    await act(async () => root.render(<QueryClientProvider client={qc}><Probe /></QueryClientProvider>))
    await act(async () => Stream.current.dispatchEvent(new MessageEvent('data_updated', { data: JSON.stringify({ datasets: ['ext_data'] }) })))
    for (const key of keys.slice(0, 3)) expect(qc.getQueryState(key)?.isInvalidated).toBe(true)
    expect(qc.getQueryState(keys[3])?.isInvalidated).toBe(false)
    await act(async () => Stream.current.dispatchEvent(new MessageEvent('data_updated', { data: JSON.stringify({ datasets: ['mainline'] }) })))
    expect(qc.getQueryState(keys[4])?.isInvalidated).toBe(true)
    expect(qc.getQueryState(keys[5])?.isInvalidated).toBe(true)
    expect(qc.getQueryState(keys[3])?.isInvalidated).toBe(false)
    await act(async () => Stream.current.dispatchEvent(new MessageEvent('data_updated', { data: JSON.stringify({ datasets: ['regime'] }) })))
    for (const key of keys.slice(3, 6)) expect(qc.getQueryState(key)?.isInvalidated).toBe(true)
    for (const key of keys.slice(6)) expect(qc.getQueryState(key)?.isInvalidated).toBe(false)
  } finally { await act(async () => root.unmount()); qc.clear(); vi.unstubAllGlobals() }
})
it('refreshes only the updated dataset even when realtime display is off, and catches up on connection', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('EventSource', Stream)
  const qc = new QueryClient()
  for (const key of [['kline-minute', '600000.SH'], ['financials', 'status'], ['pipeline-jobs'], ['review-publication', 'today'], ['factors-library', 'all'], ['screener-cached'], ['unrelated']]) qc.setQueryData(key, { value: 1 })
  const host = document.createElement('div'); const root = createRoot(host)
  try {
    await act(async () => root.render(<QueryClientProvider client={qc}><Probe /></QueryClientProvider>))
    await act(async () => Stream.current.dispatchEvent(new MessageEvent('data_updated', { data: JSON.stringify({ datasets: ['minute'] }) })))
    expect(qc.getQueryState(['kline-minute', '600000.SH'])?.isInvalidated).toBe(true)
    expect(qc.getQueryState(['financials', 'status'])?.isInvalidated).toBe(false)
    expect(qc.getQueryState(['pipeline-jobs'])?.isInvalidated).toBe(true)
    expect(qc.getQueryState(['factors-library', 'all'])?.isInvalidated).toBe(false)
    expect(qc.getQueryState(['screener-cached'])?.isInvalidated).toBe(false)
    await act(async () => Stream.current.dispatchEvent(new MessageEvent('data_updated', { data: JSON.stringify({ datasets: ['config'] }) })))
    expect(qc.getQueryState(['factors-library', 'all'])?.isInvalidated).toBe(true)
    expect(qc.getQueryState(['screener-cached'])?.isInvalidated).toBe(true)
    await act(async () => Stream.current.dispatchEvent(new MessageEvent('data_updated', { data: JSON.stringify({ datasets: ['all'] }) })))
    expect(qc.getQueryState(['review-publication', 'today'])?.isInvalidated).toBe(true)
    await act(async () => Stream.current.onopen?.())
    expect(qc.getQueryState(['financials', 'status'])?.isInvalidated).toBe(true)
    expect(qc.getQueryState(['review-publication', 'today'])?.isInvalidated).toBe(true)
    expect(qc.getQueryState(['unrelated'])?.isInvalidated).toBe(false)
  } finally { await act(async () => root.unmount()); qc.clear(); vi.unstubAllGlobals() }
})

beforeEach(() => activateSession('test-user'))

it('refreshes only current Beijing-day strategy details and coalesces quote events', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers()
  // UTC 已跨到 22 日，北京仍是当天 10 点；日期键明确按北京时间。
  vi.setSystemTime(new Date('2026-10-22T02:00:00Z'))
  vi.stubGlobal('EventSource', Stream)
  const qc = new QueryClient()
  const current = [QK.screenerCached('2026-10-22'), QK.screenerCachedResult('a', '2026-10-22')]
  const unchanged = [QK.screenerCached('2026-10-21'), QK.screenerCachedResult('a', '2026-10-21'), QK.screenerCachedSummary, QK.screenerCached()]
  const keys = [...current, ...unchanged]
  const reset = () => keys.forEach(key => qc.setQueryData(key, { value: Date.now() }))
  reset()
  const root = createRoot(document.createElement('div'))
  try {
    await act(async () => root.render(<QueryClientProvider client={qc}><LiveProbe /></QueryClientProvider>))
    await act(async () => Stream.current.dispatchEvent(new MessageEvent('quotes_updated')))
    current.forEach(key => expect(qc.getQueryState(key)?.isInvalidated).toBe(true))
    unchanged.forEach(key => expect(qc.getQueryState(key)?.isInvalidated).toBe(false))
    reset()
    await act(async () => {
      vi.advanceTimersByTime(1000)
      Stream.current.dispatchEvent(new MessageEvent('quotes_updated'))
      Stream.current.dispatchEvent(new MessageEvent('quotes_updated'))
    })
    current.forEach(key => expect(qc.getQueryState(key)?.isInvalidated).toBe(false))
    await act(async () => vi.advanceTimersByTime(5000))
    current.forEach(key => expect(qc.getQueryState(key)?.isInvalidated).toBe(true))
    unchanged.forEach(key => expect(qc.getQueryState(key)?.isInvalidated).toBe(false))
  } finally {
    await act(async () => root.unmount())
    qc.clear(); vi.unstubAllGlobals(); vi.useRealTimers()
  }
})

it('respects screener refresh opt-out and cancels delayed invalidation when disabled or unmounted', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-21T16:30:00Z')) // 北京已是 22 日。
  vi.stubGlobal('EventSource', Stream)
  const qc = new QueryClient()
  const key = QK.screenerCachedResult('a', '2026-10-22')
  qc.setQueryData(key, {})
  const root = createRoot(document.createElement('div'))
  let mounted = true
  const render = (enabled: boolean, pages?: Record<string, boolean>) => root.render(
    <QueryClientProvider client={qc}><LiveProbe enabled={enabled} pages={pages} /></QueryClientProvider>,
  )
  try {
    await act(async () => render(true, { 'screener-cached': false }))
    await act(async () => Stream.current.dispatchEvent(new MessageEvent('quotes_updated')))
    expect(qc.getQueryState(key)?.isInvalidated).toBe(false)
    await act(async () => render(true))
    await act(async () => Stream.current.dispatchEvent(new MessageEvent('quotes_updated')))
    expect(qc.getQueryState(key)?.isInvalidated).toBe(true)
    qc.setQueryData(key, {})
    await act(async () => Stream.current.dispatchEvent(new MessageEvent('quotes_updated')))
    await act(async () => render(false))
    await act(async () => vi.advanceTimersByTime(6000))
    expect(qc.getQueryState(key)?.isInvalidated).toBe(false)
    await act(async () => render(true))
    await act(async () => Stream.current.dispatchEvent(new MessageEvent('quotes_updated')))
    qc.setQueryData(key, {})
    await act(async () => Stream.current.dispatchEvent(new MessageEvent('quotes_updated')))
    await act(async () => root.unmount())
    mounted = false
    await act(async () => vi.advanceTimersByTime(6000))
    expect(qc.getQueryState(key)?.isInvalidated).toBe(false)
  } finally {
    if (mounted) await act(async () => root.unmount())
    qc.clear(); vi.unstubAllGlobals(); vi.useRealTimers()
  }
})
