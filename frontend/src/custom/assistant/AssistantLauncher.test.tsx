// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QK } from '@/lib/queryKeys'
import { activateSession } from '@/lib/sessionRuntime'
import type { AssistantEvent } from './client'
import { AssistantLauncher } from './AssistantLauncher'
import { decideAction, newSession, sendMessage } from './store'

const mocks = vi.hoisted(() => ({ stream: vi.fn(), decide: vi.fn() }))
vi.mock('./client', () => ({ assistantChatStream: mocks.stream, decideAssistantAction: mocks.decide }))
vi.mock('./ui/AssistantFloatingButton', () => ({ AssistantFloatingButton: () => null }))
vi.mock('./ui/AiConfigEntry', () => ({ AiConfigEntry: () => null }))
vi.mock('./ui/AssistantDrawer', () => ({ AssistantDrawer: () => null }))

let host: HTMLDivElement
let root: Root
let client: QueryClient
let storedSymbols: string[]
const enrichedKey = QK.watchlistEnriched('industry')
const inactiveEnrichedKey = QK.watchlistEnriched('concept')

function WatchlistProbe() {
  const list = useQuery({ queryKey: QK.watchlist, queryFn: async () => ({ symbols: [...storedSymbols] }) })
  const enriched = useQuery({
    queryKey: enrichedKey,
    queryFn: async () => ({ rows: storedSymbols.map(symbol => ({ symbol })) }),
    enabled: Boolean(list.data?.symbols.length),
  })
  return <div>{list.data?.symbols.length ?? 0}/{enriched.data?.rows.length ?? 0} {enriched.data?.rows.map(row => row.symbol).join(',')}</div>
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  activateSession('assistant-cache-test')
  newSession()
  mocks.decide.mockResolvedValue(undefined)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(QK.watchlist, { symbols: [] })
  client.setQueryData(enrichedKey, { rows: [] })
  client.setQueryData(inactiveEnrichedKey, { rows: [] })
  client.setQueryData(QK.watchlistGroups, { groups: [] })
  storedSymbols = []
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function settle() {
  for (let index = 0; index < 4; index++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

async function render(launcher = true) {
  await act(async () => root.render(<QueryClientProvider client={client}>
    {launcher && <AssistantLauncher pathname="/watchlist" collapsed={false} />}
    <WatchlistProbe />
  </QueryClientProvider>))
  await settle()
}

function toolStream(name: string, ok: boolean) {
  mocks.stream.mockImplementation(async function* () {
    yield { type: 'tool_call', call_id: 'call-1', name, args: {} } satisfies AssistantEvent
    yield { type: 'tool_result', call_id: 'call-1', name, ok, summary: ok ? '完成' : '没有执行', elapsed_ms: 1 } satisfies AssistantEvent
    yield { type: 'done' } satisfies AssistantEvent
  })
}

it('refreshes the already open watchlist only after a confirmed add succeeds, including enriched variants', async () => {
  let finish!: () => void
  const decision = new Promise<void>(resolve => { finish = resolve })
  mocks.stream.mockImplementation(async function* () {
    yield { type: 'tool_call', call_id: 'call-1', name: 'add_to_watchlist', args: { symbol: '600519.SH' } } satisfies AssistantEvent
    yield { type: 'action_confirm', call_id: 'call-1', name: 'add_to_watchlist', label: '添加自选', risk: '', expires_in: 120 } satisfies AssistantEvent
    await decision
    storedSymbols = ['600519.SH']
    yield { type: 'tool_result', call_id: 'call-1', name: 'add_to_watchlist', ok: true, summary: '已添加', elapsed_ms: 1 } satisfies AssistantEvent
    yield { type: 'done' } satisfies AssistantEvent
  })
  await render()
  await act(async () => sendMessage('添加贵州茅台到自选'))
  await settle()
  expect(host.textContent).toBe('0/0 ')
  expect(client.getQueryState(QK.watchlist)?.isInvalidated).toBe(false)
  await act(async () => { await decideAction('call-1', true); finish() })
  await settle()
  expect(host.textContent).toBe('1/1 600519.SH')
  expect(client.getQueryState(inactiveEnrichedKey)?.isInvalidated).toBe(true)
  expect(client.getQueryState(QK.watchlistGroups)?.isInvalidated).toBe(false)
})

it.each([
  ['add_to_watchlist', false],
  ['get_watchlist', true],
] as const)('does not refresh watchlist for tool %s with success=%s', async (name, ok) => {
  toolStream(name, ok)
  await render()
  storedSymbols = ['600519.SH']
  await act(async () => sendMessage('测试工具结果'))
  await settle()
  expect(host.textContent).toBe('0/0 ')
  expect(client.getQueryState(QK.watchlist)?.isInvalidated).toBe(false)
  expect(client.getQueryState(enrichedKey)?.isInvalidated).toBe(false)
  expect(client.getQueryState(inactiveEnrichedKey)?.isInvalidated).toBe(false)
})

it('cleans up the host subscription and does not replay saved successful tool results on remount', async () => {
  toolStream('add_to_watchlist', true)
  await render()
  await render(false)
  storedSymbols = ['600519.SH']
  await act(async () => sendMessage('宿主已卸载'))
  await settle()
  expect(host.textContent).toBe('0/0 ')
  expect(client.getQueryState(inactiveEnrichedKey)?.isInvalidated).toBe(false)
  await render()
  expect(host.textContent).toBe('0/0 ')
  await act(async () => sendMessage('重新挂载后添加'))
  await settle()
  expect(host.textContent).toBe('1/1 600519.SH')
})
