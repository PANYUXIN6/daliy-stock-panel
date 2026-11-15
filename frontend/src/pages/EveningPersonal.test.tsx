// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { EveningPersonalHistory, EveningPersonalRecord, User } from '@/lib/api'
import { AuthContext } from '@/lib/auth'
import { QK } from '@/lib/queryKeys'
import { EveningHistory } from './EveningHistory'
import { EveningFeedbackOverview } from './EveningFeedbackOverview'

const mocks = vi.hoisted(() => ({ history: vi.fn(), overview: vi.fn(), feedback: vi.fn() }))
vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), api: { eveningHistory: mocks.history, eveningFeedbackOverview: mocks.overview, eveningFeedback: mocks.feedback } }))
let root: Root, host: HTMLDivElement, client: QueryClient
let history: EveningPersonalHistory
const record = (day: string, version: string): EveningPersonalRecord => ({ id: `${version}:000001.SZ`, day, version, month: '2026-11', symbol: '000001.SZ', name: '历史股票', episode_id: 'episode-one', observation: null,
  feedback: { starred: false, outcome: null, revision: 0, updated_at: null }, started_at: '2026-11-02', last_seen: '2026-11-03', ended_at: null, first_reference: 'v1:000001.SZ', historical: false,
  performance: {
    '1': { window: 1, target_date: '2026-11-03', as_of: '2026-11-03', return: .1, status: 'evaluated', reason: null, trading_constraints: ['目标日涨停，价格表现不代表可成交'], data_version: 'v1' },
    '3': { window: 3, target_date: '2026-11-05', as_of: '2026-11-05', return: null, status: 'unavailable', reason: '目标日停牌', trading_constraints: [], data_version: 'v1' },
    '5': { window: 5, target_date: '2026-11-09', as_of: '2026-11-05', return: null, status: 'not_due', reason: '目标交易日未到', trading_constraints: [], data_version: 'v1' },
  } })
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity, staleTime: Infinity } } })
  history = { user_id: 'a', records: [record('2026-11-03', 'v2'), record('2026-11-02', 'v1')], events: [{ request_id: 'request-1', episode_id: 'episode-one', reference_id: 'v1:000001.SZ', month: '2026-11', at: '2026-11-03T10:00:00Z', actions: ['star_set', 'star_cancel'], revision: 2 }] }
  mocks.history.mockReset().mockImplementation(async () => history)
  mocks.feedback.mockReset().mockImplementation(async request => {
    const feedback = { starred: request.starred, outcome: request.outcome, revision: request.expected_revision + 1, updated_at: null }
    history = { ...history, records: history.records.map(row => ({ ...row, feedback })) }
    return { ...feedback, episode_id: 'episode-one' }
  })
  mocks.overview.mockReset().mockResolvedValue({ user_id: 'a', episode_count: 2, recommendation_count: 5, starred: 0, unstarred: 2, profit: 0, loss: 0, unmarked: 2,
    performance: Object.fromEntries(['1', '3', '5'].map(window => [window, { total: 2, evaluated: 1, not_due: 1, unavailable: 0, mean_return: .1 }])) })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
async function settle() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 25)) }) }
async function render(page: 'history' | 'overview' = 'history', userId = 'a') {
  await act(async () => root.render(<AuthContext.Provider value={{ id: userId, role: 'guest' } as User}><QueryClientProvider client={client}><MemoryRouter>{page === 'history' ? <EveningHistory /> : <EveningFeedbackOverview />}</MemoryRouter></QueryClientProvider></AuthContext.Provider>))
  await settle(); await settle()
}
async function click(text: string) { const button = [...host.querySelectorAll('button')].find(button => button.textContent === text)!; expect(button).toBeTruthy(); await act(async () => button.click()); await settle() }

it('历史保留原报告引用、窗口状态与取消事件，同过程反馈同步而不重复创建', async () => {
  await render()
  expect(host.querySelectorAll('[data-history-stock]')).toHaveLength(2)
  expect(host.textContent).toContain('+10.00%')
  expect(host.textContent).toContain('不可评价')
  expect(host.textContent).toContain('未到期')
  expect(host.querySelector('a[href="/evening-report?date=2026-11-02&version=v1"]')).not.toBeNull()
  await click('过程与反馈变更')
  expect(host.textContent).toContain('取消星标')
  expect(host.textContent).toContain('设置星标')
  expect(host.textContent).toContain('首次推荐锚点')
  expect(host.textContent).toContain('目标日涨停，价格表现不代表可成交')
  expect(host.textContent).toContain('旧版未记录')
  const buttons = [...host.querySelectorAll('button')].filter(button => button.getAttribute('aria-label')?.includes('符合我的审美'))
  await act(async () => buttons[0].click()); await settle(); await settle()
  expect(mocks.feedback).toHaveBeenCalledTimes(1)
  expect(buttons.every(button => button.getAttribute('aria-pressed') === 'true')).toBe(true)
  expect(history.records).toHaveLength(2)
})

it('无反馈时概览不制造负样本或盈亏，价格表现有独立分母', async () => {
  await render('overview')
  expect(host.querySelector('[aria-label="审美反馈"]')?.textContent).toContain('未星标2')
  expect(host.querySelector('[aria-label="自报盈亏"]')?.textContent).toContain('未标记2')
  expect(host.querySelector('[aria-label="自报盈亏"]')?.textContent).toContain('已标记 0 / 2')
  expect(host.querySelector('[aria-label="客观观察表现"]')?.textContent).toContain('+10.00%')
  expect(host.textContent).toContain('均值仅基于该窗口可评价样本')
  expect(mocks.feedback).not.toHaveBeenCalled()
})

it('切换账号读取独立查询，准备期间不显示前账号历史', async () => {
  await render()
  const firstMonth = mocks.history.mock.calls[0][0]
  expect(client.getQueryData(QK.eveningHistory('a', firstMonth))).toBeTruthy()
  let complete!: (value: EveningPersonalHistory) => void
  mocks.history.mockImplementationOnce(() => new Promise(resolve => { complete = resolve }))
  await render('history', 'b')
  expect(host.textContent).not.toContain('历史股票')
  await act(async () => complete({ user_id: 'b', records: [], events: [] })); await settle()
  expect(host.textContent).toContain('本月暂无观察记录')
  expect(client.getQueryData(QK.eveningHistory('a', firstMonth))).toBeTruthy()
  expect(client.getQueryData(QK.eveningHistory('b', firstMonth))).toEqual({ user_id: 'b', records: [], events: [] })
})
