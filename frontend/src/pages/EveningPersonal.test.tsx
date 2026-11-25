// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { EveningFeedbackOverview as Overview, EveningPersonalHistory, EveningPersonalRecord, User } from '@/lib/api'
import { AuthContext } from '@/lib/auth'
import { QK } from '@/lib/queryKeys'
import { EveningHistory } from './EveningHistory'
import { EveningFeedbackOverview } from './EveningFeedbackOverview'

const mocks = vi.hoisted(() => ({ history: vi.fn(), overview: vi.fn(), feedback: vi.fn() }))
vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), api: { eveningHistory: mocks.history, eveningFeedbackOverview: mocks.overview, eveningFeedback: mocks.feedback } }))
let root: Root, host: HTMLDivElement, client: QueryClient
let history: EveningPersonalHistory
const record = (day: string, version: string): EveningPersonalRecord => ({ id: `${version}:000001.SZ`, day, version, report_kind: 'original', month: '2026-11', symbol: '000001.SZ', name: '历史股票', episode_id: 'episode-one', observation: null,
  feedback: { starred: false, outcome: null, updated_at: null }, started_at: '2026-11-02', last_seen: '2026-11-03', ended_at: null, first_reference: 'v1:000001.SZ', historical: false,
  performance: {
    '1': { window: 1, target_date: '2026-11-03', as_of: '2026-11-03', return: .1, status: 'evaluated', reason: null, trading_constraints: ['目标日涨停，价格表现不代表可成交'], data_version: 'v1' },
    '3': { window: 3, target_date: '2026-11-05', as_of: '2026-11-05', return: null, status: 'unavailable', reason: '目标日停牌', trading_constraints: [], data_version: 'v1' },
    '5': { window: 5, target_date: '2026-11-09', as_of: '2026-11-05', return: null, status: 'not_due', reason: '目标交易日未到', trading_constraints: [], data_version: 'v1' },
  } })
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity, staleTime: Infinity } } })
  history = { user_id: 'a', records: [record('2026-11-03', 'v2'), record('2026-11-02', 'v1')] }
  mocks.history.mockReset().mockImplementation(async () => history)
  mocks.feedback.mockReset().mockImplementation(async request => {
    const feedback = { starred: request.starred, outcome: request.outcome, updated_at: null }
    history = { ...history, records: history.records.map(row => ({ ...row, feedback })) }
    return { ...feedback, episode_id: 'episode-one' }
  })
  mocks.overview.mockReset().mockResolvedValue({ user_id: 'a', episode_count: 2, recommendation_count: 5, starred: 0, unstarred: 2, profit: 0, loss: 0, unmarked: 2,
    performance: Object.fromEntries(['1', '3', '5'].map(window => [window, { total: 2, evaluated: 1, not_due: 0, overdue_pending_review: 1, unavailable: 0, mean_return: .1 }])) })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
async function settle() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 25)) }) }
async function render(page: 'history' | 'overview' = 'history', userId = 'a') {
  await act(async () => root.render(<AuthContext.Provider value={{ id: userId, role: 'guest' } as User}><QueryClientProvider client={client}><MemoryRouter>{page === 'history' ? <EveningHistory /> : <EveningFeedbackOverview />}</MemoryRouter></QueryClientProvider></AuthContext.Provider>))
  await settle(); await settle()
}
async function click(text: string) { const button = [...host.querySelectorAll('button')].find(button => button.textContent === text)!; expect(button).toBeTruthy(); await act(async () => button.click()); await settle() }

it('历史保留原报告链接及到期窗口状态，同过程反馈同步而不重复创建', async () => {
  await render()
  expect(host.querySelectorAll('[data-history-stock]')).toHaveLength(2)
  expect(host.textContent).toContain('+10.00%')
  expect(host.textContent).toContain('不可评价')
  expect(host.textContent).toContain('未到期')
  expect(host.querySelector('a[href="/evening-report?date=2026-11-02&version=v1"]')).not.toBeNull()
  await click('查看跟踪过程')
  expect(host.textContent).toContain('本月同一过程的记录')
  expect(host.textContent).toContain('首次推荐锚点')
  expect(host.textContent).toContain('目标日涨停，价格表现不代表可成交')
  expect(host.textContent).toContain('题材未记录')
  const buttons = [...host.querySelectorAll('button')].filter(button => button.getAttribute('aria-label')?.includes('符合我的审美'))
  await act(async () => buttons[0].click()); await settle(); await settle()
  expect(mocks.feedback).toHaveBeenCalledTimes(1)
  expect(buttons.every(button => button.getAttribute('aria-pressed') === 'true')).toBe(true)
  expect(history.records).toHaveLength(2)
  const starFilter = host.querySelector<HTMLSelectElement>('[aria-label="星标"]')!
  await act(async () => { starFilter.value = 'starred'; starFilter.dispatchEvent(new Event('change', { bubbles: true })) })
  expect(host.querySelectorAll('[data-history-stock]')).toHaveLength(2)
  await act(async () => buttons[0].click()); await settle(); await settle()
  expect(host.textContent).toContain('没有符合当前筛选的观察记录')
  expect(host.querySelectorAll('[data-history-stock]')).toHaveLength(0)
  await click('清除筛选')
  expect(host.querySelectorAll('[data-history-stock]')).toHaveLength(2)
  expect(history.records.every(row => !row.feedback.starred)).toBe(true)
})

it('无反馈时概览不制造负样本或盈亏，价格表现有独立分母', async () => {
  await render('overview')
  expect(host.querySelector('[aria-label="审美反馈"]')?.textContent).toContain('未星标2')
  expect(host.querySelector('[aria-label="自报盈亏"]')?.textContent).toContain('未标记2')
  expect(host.querySelector('[aria-label="自报盈亏"]')?.textContent).toContain('已标记 0 / 2')
  expect(host.querySelector('[aria-label="客观观察表现"]')?.textContent).toContain('+10.00%')
  expect(host.textContent).toContain('均值仅基于该窗口可评价样本')
  const table = host.querySelector('[aria-label="客观观察表现"] table')!
  const pending = [...table.querySelectorAll('thead th')].findIndex(cell => cell.textContent === '已到期，待回看')
  expect(pending).toBeGreaterThan(0)
  expect(table.querySelector('tbody tr')?.children[pending]?.textContent).toBe('1')
  expect(mocks.feedback).not.toHaveBeenCalled()
})

it('观察历史按归档决定展示题材内名次和名额，名次随对应记录一起排序', async () => {
  const [current, other] = history.records
  current.observation = {
    role: 'fallback_leader', role_status: 'review_pending', role_by_history_tracking: 'true_leader', board: 'main',
    primary_theme: { id: 'theme', name: '示例题材', strength: 50 }, related_themes: [], values: {}, variables: {},
    dip: { status: 'none', reasons: [], gates: {}, e1: {}, e2: {} }, reason: '辨识度32分达标',
    decision: {
      steps: [], first_blocking_step: null, blocking_reason_code: null, all_reason_codes: [],
      selected: true, comparison_eligible: true, comparison_group: [], competed_quota_type: 'leader',
      occupied_quota_type: 'leader', quota_fallback_allowed: false, rank_within_theme_role: 2, rank_global_role: 5,
      quota_limit: 2, sorting_rule_version: 'v2', competed_themes: ['theme'], final_theme: 'theme',
      dedup_rule_version: 'v2', dedup_decision_basis: 'theme_strength', merged_into_row_id: null,
    },
  }
  const { decision, ...observation } = current.observation
  other.observation = { ...observation, decision: { ...decision!, rank_within_theme_role: 1, quota_limit: 1 } }
  await render()
  const ranks = () => [...host.querySelectorAll('tbody tr')].map(row => row.textContent?.match(/分配前第\d+名（\d+个名额）/)?.[0])
  expect(ranks()).toEqual(['分配前第2名（2个名额）', '分配前第1名（1个名额）'])
  await act(async () => host.querySelector<HTMLElement>('th[aria-sort]')!.click())
  expect(ranks()).toEqual(['分配前第1名（1个名额）', '分配前第2名（2个名额）'])
})

it('端点完成后才补齐期间路径时分别展示两次评价日期', async () => {
  history.records[0].performance['3'] = {
    ...history.records[0].performance['3'], status: 'evaluated', return: .02, reason: null, as_of: '2026-11-12',
    path: { status: 'evaluated', as_of: '2026-11-13', max_favorable_return: .04, max_adverse_return: -.03,
      valid_count: 3, expected_count: 3, missing_count: 0, suspended_count: 0 },
  }
  await render()
  await click('查看跟踪过程')
  expect(host.textContent).toContain('端点收益截至 2026-11-12')
  expect(host.textContent).toContain('期间路径截至 2026-11-13')
})

it('历史表格和详情显示已到期但尚待回看', async () => {
  history.records[0].performance['3'] = { ...history.records[0].performance['3'], status: 'overdue_pending_review', reason: '最近评价截至 2026-11-04' }
  await render()
  expect(host.textContent).toContain('已到期，待回看')
  await click('查看跟踪过程')
  expect(host.textContent?.match(/已到期，待回看/g)).toHaveLength(2)
})

it('打开历史过程详情会替换仍在进行的盘前请求', async () => {
  const close = Date.parse('2026-11-09T15:00:00+08:00')
  const clock = vi.spyOn(Date, 'now').mockReturnValue(close - 2_000)
  let previousRequest: { signal: AbortSignal; finish: () => void } | undefined
  mocks.history.mockImplementation((_month: string, signal: AbortSignal) => {
    const status = Date.now() >= close ? 'overdue_pending_review' : 'not_due'
    const response = { ...history, records: history.records.map(row => ({ ...row, performance: {
      ...row.performance, '5': { ...row.performance['5'], status },
    } })) }
    if (mocks.history.mock.calls.length !== 2) return Promise.resolve(response)
    return new Promise(resolve => { previousRequest = { signal, finish: () => resolve(response) } })
  })
  try {
    await render()
    await act(async () => { void client.invalidateQueries({ queryKey: QK.eveningHistory('a', mocks.history.mock.calls[0][0]) }) })
    await settle()
    expect(previousRequest).toBeDefined()
    clock.mockReturnValue(close + 1_000)
    await click('查看跟踪过程')
    expect(mocks.history).toHaveBeenCalledTimes(3)
    expect(previousRequest!.signal.aborted).toBe(true)
    await act(async () => previousRequest!.finish())
    await settle()
    expect(host.querySelector('[aria-label="历史股票跟踪过程"]')?.textContent).toContain('已到期，待回看')
  } finally {
    clock.mockRestore()
  }
})

it.each(['history', 'overview'] as const)('收盘后重新打开 %s 会刷新仍在新鲜期的盘前缓存', async page => {
  const clock = vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-11-09T14:59:58+08:00'))
  const endpoint = mocks[page]
  const options = client.getDefaultOptions()
  client.setDefaultOptions({ ...options, queries: { ...options.queries, staleTime: 5_000 } })
  try {
    await render(page)
    expect(endpoint).toHaveBeenCalledTimes(1)
    await act(async () => root.render(null))
    clock.mockReturnValue(Date.parse('2026-11-09T15:00:01+08:00'))
    if (page === 'history') {
      history = { ...history, records: history.records.map(row => ({ ...row, performance: {
        ...row.performance, '5': { ...row.performance['5'], status: 'overdue_pending_review' },
      } })) }
    } else {
      const overview = client.getQueryData<Overview>(QK.eveningFeedbackOverview('a'))!
      mocks.overview.mockResolvedValue({ ...overview, performance: {
        ...overview.performance, '5': { ...overview.performance['5'], overdue_pending_review: 2 },
      } })
    }
    await render(page)
    expect(endpoint).toHaveBeenCalledTimes(2)
    if (page === 'history') expect(host.textContent).toContain('已到期，待回看')
    else expect(host.querySelector('tbody tr:last-child')?.children[4]?.textContent).toBe('2')
  } finally {
    clock.mockRestore()
  }
})

it('切换账号读取独立查询，准备期间不显示前账号历史', async () => {
  await render()
  const firstMonth = mocks.history.mock.calls[0][0]
  expect(client.getQueryData(QK.eveningHistory('a', firstMonth))).toBeTruthy()
  let complete!: (value: EveningPersonalHistory) => void
  mocks.history.mockImplementationOnce(() => new Promise(resolve => { complete = resolve }))
  await render('history', 'b')
  expect(host.textContent).not.toContain('历史股票')
  await act(async () => complete({ user_id: 'b', records: [] })); await settle()
  expect(host.textContent).toContain('本月暂无观察记录')
  expect(client.getQueryData(QK.eveningHistory('a', firstMonth))).toBeTruthy()
  expect(client.getQueryData(QK.eveningHistory('b', firstMonth))).toEqual({ user_id: 'b', records: [] })
})
