// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { StrategyBundlePreview } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { StrategyBundleDialog } from './StrategyBundleDialog'

const fixtures = vi.hoisted(() => ({ inspect: vi.fn(), apply: vi.fn(), export: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { strategyInspectBundle: fixtures.inspect, strategyApplyBundle: fixtures.apply, strategyExportBundle: fixtures.export } }))
vi.mock('@/components/Modal', () => ({ Modal: ({ children }: { children: React.ReactNode }) => <div>{children}</div> }))
let host: HTMLDivElement
let root: Root
let client: QueryClient
const closed = vi.fn()
const imported = vi.fn()

function preview(canApply = true): StrategyBundlePreview {
  return { strategy_id: 'composite_pack', label: '叠加包', counts: { strategies: 1 }, warnings: [],
    missing_deps: canApply ? [] : ['缺少子策略: leaf'], can_apply: canApply,
    entries: [{ path: 'strategies/composite/composite_pack.json', kind: 'strategies', id: 'composite_pack', action: 'create', bytes: 80 }] }
}
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  fixtures.inspect.mockReset().mockResolvedValue(preview())
  fixtures.apply.mockReset().mockResolvedValue({ imported: 1, skipped: 0, failed: 0, strategy_id: 'composite_pack' })
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(QK.screenerStrategies(), { strategies: [] })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
async function settle() {
  for (let n = 0; n < 4; n++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function mount() {
  await act(async () => root.render(<QueryClientProvider client={client}><StrategyBundleDialog strategies={[{ id: 'leaf', name: '维护者策略' }]} onClose={closed} onImported={imported} /></QueryClientProvider>))
}
async function choose() {
  const input = host.querySelector<HTMLInputElement>('input[type=file]')!
  const file = new File(['zip'], 'pack.zip', { type: 'application/zip' })
  Object.defineProperty(input, 'files', { configurable: true, value: [file] })
  await act(async () => input.dispatchEvent(new Event('change', { bubbles: true })))
  await settle()
  return file
}

it('blocks import when preview reports a missing dependency or conflict', async () => {
  fixtures.inspect.mockResolvedValue(preview(false))
  await mount(); await choose()
  expect(host.textContent).toContain('缺少子策略: leaf')
  const apply = [...host.querySelectorAll('button')].find(button => button.textContent === '确认无覆盖导入')!
  expect(apply.disabled).toBe(true)
  await act(async () => apply.click())
  expect(fixtures.apply).not.toHaveBeenCalled()
})

it('imports only after preview confirmation and invalidates current-user strategy queries', async () => {
  await mount()
  const file = await choose()
  expect(fixtures.apply).not.toHaveBeenCalled()
  const apply = [...host.querySelectorAll('button')].find(button => button.textContent === '确认无覆盖导入')!
  await act(async () => apply.click()); await settle()
  expect(fixtures.apply).toHaveBeenCalledWith(file)
  expect(imported).toHaveBeenCalledWith('composite_pack')
  expect(closed).toHaveBeenCalledOnce()
  expect(client.getQueryState(QK.screenerStrategies())?.isInvalidated).toBe(true)
})

it('shows preview errors and keeps import unavailable', async () => {
  fixtures.inspect.mockRejectedValue(new Error('不支持Python源码包'))
  await mount(); await choose()
  expect(host.querySelector('[role=alert]')?.textContent).toContain('不支持Python源码包')
  expect(host.querySelector('[aria-label="策略包预览"]')).toBeNull()
})
