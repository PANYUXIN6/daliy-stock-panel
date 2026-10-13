// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, it, vi } from 'vitest'
import { useQuoteStream } from './useQuoteStream'
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
