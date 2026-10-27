// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { type MonitorRule, type StockLevels } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { PriceAlertDialog } from './PriceAlertDialog'

vi.mock('@/lib/useSharedQueries', () => ({ usePreferences: () => ({ data: {} }) }))

let host: HTMLDivElement
let root: ReturnType<typeof createRoot>
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

it('推荐和默认选中可展示的 Keltner 价位，既有 ATR 点位提醒仍可查看', async () => {
  const symbol = '600000.SH'
  const levels: StockLevels = {
    symbol, close: 20, summary: '',
    levels: {
      sr: [], pivot: [], extreme: [], boll: [],
      keltner_s: [{ value: 22, label: 'Keltner短上', type: 'keltner_s', side: 'resistance' }],
      keltner_m: [], keltner_l: [],
      atr_stop: [{ value: 21, label: 'ATR上轨', type: 'atr_stop', side: 'resistance' }],
      gap: [], fib: [], round: [],
    },
  }
  const existing: MonitorRule = {
    id: 'existing-atr', name: '点位提醒 · ATR上轨', enabled: true, type: 'price',
    scope: 'symbols', symbols: [symbol], direction: 'entry',
    conditions: [{ field: 'close', op: '>=', value: 21 }],
    logic: 'and', cooldown_seconds: 3600, severity: 'warn', message: '',
  }
  client.setQueryData(QK.stockLevels(symbol), levels)
  client.setQueryData(QK.monitorRules, { rules: [existing] })
  await act(async () => root.render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <QueryClientProvider client={client}>
        <PriceAlertDialog symbol={symbol} name="浦发银行" onClose={() => {}} />
      </QueryClientProvider>
    </MemoryRouter>,
  ))
  expect(host.querySelector<HTMLInputElement>('input[type="number"]')?.value).toBe('22.00')
  expect(host.textContent).toContain('Keltner短上')
  expect(host.textContent).not.toContain('ATR')
  const existingTab = [...host.querySelectorAll('button')].find(button => button.textContent === '已有提醒 1')!
  await act(async () => existingTab.click())
  expect(host.textContent).toContain('点位提醒 · ATR上轨')
  expect(host.textContent).toContain('涨至 21.00')
  expect(host.querySelector('[role="switch"]')?.getAttribute('aria-checked')).toBe('true')
})
