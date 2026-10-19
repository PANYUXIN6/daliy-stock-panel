// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { PaperCompareRow, User } from '@/lib/api'
import { AuthContext } from '@/lib/auth'
import { QK } from '@/lib/queryKeys'
import { activateSession, accountStorage } from '@/lib/sessionRuntime'
import { Paper } from './Paper'

const mocks = vi.hoisted(() => ({
  paperCompare: vi.fn(async () => ({ accounts: [] as PaperCompareRow[] })),
  screenerStrategies: vi.fn(async () => ({ presets: [{ id: 'strategy-a', name: '策略甲' }] })),
  monitorRulesList: vi.fn(async () => ({ rules: [{ id: 'rule-a', name: '规则甲' }] })),
  paperArenaCreate: vi.fn(async (_body: unknown) => ({ created: [{ account: 'new-account' }] })),
}))
vi.mock('@/lib/api', () => ({ api: mocks }))
vi.mock('@/lib/useSharedQueries', () => ({ useQuoteStatus: () => ({ data: undefined }) }))

const mounted: Array<() => Promise<void>> = []

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  localStorage.clear()
  activateSession('alice')
  vi.clearAllMocks()
})

afterEach(async () => {
  for (const cleanup of mounted.splice(0)) await cleanup()
})

async function renderPaper(accounts: PaperCompareRow[] = [], role: User['role'] = 'admin') {
  const host = document.createElement('div')
  host.id = 'root'
  document.body.append(host)
  const root = createRoot(host)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(QK.paperCompare, { accounts })
  mounted.push(async () => {
    await act(async () => root.unmount())
    client.clear()
    host.remove()
  })
  await act(async () => root.render(<AuthContext.Provider value={{ id: 'alice', role } as User}><QueryClientProvider client={client}><Paper /></QueryClientProvider></AuthContext.Provider>))
  return { host, client }
}

function button(scope: ParentNode, text: string) {
  const found = [...scope.querySelectorAll('button')].find(b => b.textContent?.trim() === text)
  expect(found, `button: ${text}`).toBeDefined()
  return found!
}

async function click(scope: ParentNode, text: string) {
  await act(async () => button(scope, text).click())
}

async function batchWithStrategyAndRule() {
  const { client } = await renderPaper()
  // Seed source query caches so assertions exercise the form without request timing.
  client.setQueryData(QK.screenerStrategies('any', 'all'), await mocks.screenerStrategies())
  client.setQueryData(QK.monitorRules, await mocks.monitorRulesList())
  await click(document, '批量创建账户')
  const dialog = document.querySelector('[role="dialog"]')!
  await click(dialog, '添加')
  await click(dialog, '监控规则')
  await click(dialog, '添加')
  return dialog
}

it('batch strategies retain automatic exits after adding and removing a monitor rule', async () => {
  const dialog = await batchWithStrategyAndRule()
  const ruleRow = dialog.querySelector('[title="rule-a"]')!.parentElement!
  await act(async () => (ruleRow.querySelector('button[title="移除"]') as HTMLButtonElement).click())
  expect(dialog.textContent).toContain('入场信号买入 · 出场信号全平')
  await click(dialog, '创建 1 个账户')
  expect(mocks.paperArenaCreate).toHaveBeenCalledWith(expect.objectContaining({
    sources: [expect.objectContaining({ match_kind: 'strategy', match_id: 'strategy-a', side: 'auto' })],
  }))
})

it.each(['guest', 'pro'] as const)('hides monitor sources and skips rule queries for %s', async role => {
  const { client } = await renderPaper([], role)
  await act(async () => { client.setQueryData(QK.screenerStrategies('any', 'all'), await mocks.screenerStrategies()) })
  await click(document, '批量创建账户')
  const dialog = document.querySelector('[role="dialog"]')!
  expect([...dialog.querySelectorAll('button')].some(b => b.textContent === '监控规则')).toBe(false)
  expect(mocks.monitorRulesList).not.toHaveBeenCalled()
  await click(dialog, '添加')
  expect(dialog.querySelector('[title="strategy-a"]')).not.toBeNull()
})

it.each(['buy', 'sell'] as const)('mixed batch uses automatic strategy direction and fixed %s monitor direction', async side => {
  const dialog = await batchWithStrategyAndRule()
  await click(dialog, side === 'buy' ? '买入' : '卖出')
  await click(dialog, '创建 2 个账户')
  expect(mocks.paperArenaCreate).toHaveBeenCalledWith(expect.objectContaining({
    sources: [
      expect.objectContaining({ match_kind: 'strategy', side: 'auto' }),
      expect.objectContaining({ match_kind: 'rule', side }),
    ],
  }))
})

function compareRow(account: string, overrides: Partial<PaperCompareRow>): PaperCompareRow {
  return {
    account, name: account, status: 'active', initial_cash: 1000,
    fees: { commission_pct: 0, stamp_tax_pct: 0, slippage_bps: 0 },
    total: 1000, cash: 1000, market_value: 0, total_pnl: 0, pnl_pct: null,
    rounds: 0, win_rate: 0, profit_loss_ratio: null, avg_holding_days: 0,
    realized_pnl: 0, max_drawdown: null, nav: [], ...overrides,
  }
}

const comparisonAccounts = () => [
  compareRow('实时盈利', { total: 800, pnl_pct: -20, estimating: true, live_total: 1200, live_pnl_pct: 20 }),
  compareRow('实时亏损', { total: 1300, pnl_pct: 30, estimating: true, live_total: 900, live_pnl_pct: -10 }),
  compareRow('已定版', { total: 1050, pnl_pct: 5, estimating: false, live_total: 2000, live_pnl_pct: 100 }),
  compareRow('仅实时收益', { total: 1000, pnl_pct: null, estimating: true, live_total: 1150, live_pnl_pct: 15 }),
  compareRow('实时缺值回退', { total: 1100, pnl_pct: 10, estimating: true, live_total: null, live_pnl_pct: null }),
  compareRow('暂无收益', {}),
]

function tableRows(host: HTMLElement) {
  return [...host.querySelectorAll('tbody tr')].map(row => [...row.querySelectorAll('td')].map(cell => cell.textContent))
}

function accountOrder(host: HTMLElement) {
  return [...host.querySelectorAll('tbody tr')].map(row => row.querySelector('td:nth-child(2) span')?.textContent)
}

it('comparison return sorting follows displayed live values, fallback values and nulls in both directions', async () => {
  const { host, client } = await renderPaper(comparisonAccounts())
  expect(accountOrder(host)).toEqual(['实时盈利', '仅实时收益', '实时缺值回退', '已定版', '实时亏损', '暂无收益'])
  expect(tableRows(host)[0][3]).toBe('+20.00%')
  expect(tableRows(host)[4][3]).toBe('-10.00%')
  expect(host.textContent).toContain('+20.00% 高于第2名 5.00pct')
  await click(host, '累计收益')
  expect(accountOrder(host)).toEqual(['实时亏损', '已定版', '实时缺值回退', '仅实时收益', '实时盈利', '暂无收益'])

  const updated = comparisonAccounts().map(row => row.account === '实时亏损' ? { ...row, live_pnl_pct: 25 } : row)
  await act(async () => {
    client.setQueryData(QK.paperCompare, { accounts: updated })
    await new Promise(resolve => setTimeout(resolve, 0)) // TanStack Query batches observer notifications.
  })
  expect(accountOrder(host)).toEqual(['已定版', '实时缺值回退', '仅实时收益', '实时盈利', '实时亏损', '暂无收益'])
})

it('comparison asset sorting follows the same live or finalized total shown in the table', async () => {
  const { host } = await renderPaper(comparisonAccounts())
  await click(host, '总资产')
  expect(accountOrder(host)).toEqual(['实时盈利', '仅实时收益', '实时缺值回退', '已定版', '暂无收益', '实时亏损'])
  expect(tableRows(host).map(cells => cells[2])).toEqual(['1,200估', '1,150估', '1,100估', '1,050', '1,000', '900估'])
  expect(host.textContent).toContain('6,400')
  await click(host, '总资产')
  expect(accountOrder(host)).toEqual(['实时亏损', '暂无收益', '已定版', '实时缺值回退', '仅实时收益', '实时盈利'])
})

it('new account defaults belong to the logged-in user, including after switching back', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  localStorage.clear()
  activateSession('alice')
  accountStorage.setItem('paper.baseDefaults.v1', JSON.stringify({ cash: '250000' }))
  localStorage.setItem('paper.baseDefaults.v1', JSON.stringify({ cash: '999999' }))
  for (const [owner, expected] of [['alice', '250000'], ['bob', '1000000'], ['alice', '250000']]) {
    activateSession(owner)
    const host = document.createElement('div')
    host.id = 'root'
    document.body.append(host)
    const root = createRoot(host)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    try {
      await act(async () => root.render(<QueryClientProvider client={client}><Paper /></QueryClientProvider>))
      const button = [...host.querySelectorAll('button')].find(b => b.textContent === '基础配置')!
      await act(async () => button.click())
      expect((document.querySelector('input[type="number"]') as HTMLInputElement).value).toBe(expected)
    } finally {
      await act(async () => root.unmount())
      client.clear()
      host.remove()
    }
  }
})
