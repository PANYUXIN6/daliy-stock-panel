// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { api, type StrategyDetail } from '@/lib/api'
import { StrategySettingsDialog } from './StrategySettingsDialog'

vi.mock('./SignalPicker', () => ({ SignalPicker: () => null }))
vi.mock('@/components/signals/SignalTriggerActions', () => ({ SignalTriggerActions: () => null }))
vi.mock('@/components/ScoringEditor', () => ({ ScoringEditor: () => null }))

const condition = { left: 'close', op: '>', right: '10', leftDays: 1 }
const detail = (id: string, overlay = [condition]) => ({
  id, name: id, source: 'builtin', execution_backend: 'polars_expr',
  params: [], params_defaults: {}, basic_filter: {}, scoring: {}, entry_signals: [], exit_signals: [],
  overlay_filter: overlay,
}) as unknown as StrategyDetail
let root: Root
let host: HTMLDivElement
let client: QueryClient

afterEach(() => {
  act(() => root?.unmount())
  host?.remove()
  client?.clear()
  vi.restoreAllMocks()
})

async function flush() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 15)) })
}

it('保存叠加条件、删除最后一条，并在切换策略时重新初始化', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(api, 'strategyGet').mockImplementation(async id => detail(id, id === 'A' ? [condition] : []))
  vi.spyOn(api, 'customSignalsOptions').mockResolvedValue({ fields: [{ key: 'close', label: '收盘价' }], operators: ['>'], kinds: [] })
  const save = vi.spyOn(api, 'strategySaveConfig').mockResolvedValue({ ok: true })
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  const render = async (id: string) => {
    await act(async () => root.render(<QueryClientProvider client={client}><StrategySettingsDialog strategyId={id} onClose={() => {}} /></QueryClientProvider>))
    await flush()
  }
  const click = async (label: string) => {
    const button = [...host.querySelectorAll('button')].find(b => b.textContent?.trim() === label)
    expect(button).toBeTruthy()
    await act(async () => button!.click())
  }
  await render('A')
  expect(host.textContent).toContain('叠加条件')
  await click('保存设置')
  expect(save).toHaveBeenLastCalledWith('A', expect.objectContaining({ overlay_filter: [condition] }))
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="删除条件"]')!.click())
  await click('保存设置')
  expect(save).toHaveBeenLastCalledWith('A', expect.objectContaining({ overlay_filter: [] }))
  await render('B')
  expect(host.textContent).toContain('无条件')
  await click('保存设置')
  expect(save).toHaveBeenLastCalledWith('B', expect.objectContaining({ overlay_filter: [] }))
})
