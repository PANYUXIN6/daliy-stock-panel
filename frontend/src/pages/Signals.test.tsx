// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { api, type CustomSignal } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { Signals } from './Signals'

vi.mock('@/components/PageHeader', () => ({ PageHeader: () => null }))
vi.mock('@/components/signals/CustomSignalDialog', () => ({ CustomSignalDialog: () => null }))

let root: Root | undefined
afterEach(() => {
  act(() => root?.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

it('renders imported numeric conditions and preserves them when toggling the signal', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const signal: CustomSignal = {
    id: 'imported', name: '导入信号', kind: 'entry', enabled: true,
    conditions: [
      { left: 'close', op: '>', right: 1 },
      { left: 'close', op: '>=', right: 0 },
      { left: 'close', op: '<', right: 'field:ma20', rightDays: 1 },
      { left: 'close', op: '>', right: '-1.5' },
    ],
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } })
  client.setQueryData(QK.customSignals, { signals: [signal] })
  client.setQueryData(QK.customSignalsOptions, { fields: [{ key: 'ma20', label: 'MA20' }] })
  const save = vi.spyOn(api, 'customSignalSave').mockResolvedValue({ ok: true, signal })
  const container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  await act(async () => root!.render(
    <QueryClientProvider client={client}><MemoryRouter><Signals /></MemoryRouter></QueryClientProvider>,
  ))
  expect(container.textContent).toContain('导入信号')
  expect(container.textContent).toContain('MA20(前1日)')
  expect(container.textContent).toContain('>=0')
  expect(container.textContent).toContain('-1.5')
  await act(async () => container.querySelector<HTMLButtonElement>('button[title="停用"]')!.click())
  expect(save).toHaveBeenCalledWith({ ...signal, enabled: false })
  client.clear()
})
