// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { AuthContext } from '@/lib/auth'
import { activateSession } from '@/lib/sessionRuntime'
import { runAiGeneration } from '@/lib/aiGeneration'
import { useAiFeature } from '@/lib/useAiCredits'
import { creditsFixture, guestUser } from '@/test/aiCredits'
import { AiCreditHint } from './AiCreditHint'
import { StockFinancialDetail } from './financials/StockFinancialDetail'

const reports = vi.hoisted(() => ({ find: vi.fn(), open: vi.fn(), start: vi.fn() }))
vi.mock('@/lib/aiReportStore', () => ({ findLatestHistoryReport: reports.find, openHistoryReport: reports.open, startAnalysis: reports.start }))
vi.mock('@/lib/useFinancials', () => {
  const query = () => ({ data: { data: [] }, isLoading: false })
  return { useFinancialMetrics: query, useFinancialIncome: query, useFinancialBalanceSheet: query,
    useFinancialCashFlow: query, useFinancialShares: query,
    useFinancialCollectionOptions: () => ({ data: { tables: [] } }),
    useFinancialFetch: () => ({ isPending: false, mutate: vi.fn() }),
  }
})

beforeEach(() => { Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }); activateSession(guestUser.id); vi.clearAllMocks() })
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })
async function settle() { for (let index = 0; index < 5; index++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) }) }

function Probe() {
  const quota = useAiFeature('assistant')
  return <><button disabled={!quota.available}>发送 · {quota.priceLabel}</button><AiCreditHint feature="assistant" /></>
}

it('按服务端价格显示，预留后减少余额，失败退点后刷新，多个入口共享一次查询', async () => {
  let credits = creditsFixture()
  credits.features.assistant.points = 9
  const fetch = vi.fn(async () => Response.json(credits))
  vi.stubGlobal('fetch', fetch)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const host = document.createElement('div'); const root = createRoot(host)
  try {
    await act(async () => root.render(<AuthContext.Provider value={guestUser}><QueryClientProvider client={client}><Probe /><AiCreditHint feature="assistant" /></QueryClientProvider></AuthContext.Provider>))
    await settle()
    expect(fetch).toHaveBeenCalledOnce()
    expect(host.textContent).toContain('发送 · 9 点')
    expect(host.textContent).toContain('本月剩余 60 / 60 点')
    let finish!: () => void
    const pending = new Promise<void>(resolve => { finish = resolve })
    const task = (async () => {
      for await (const _event of runAiGeneration('assistant', async function* () {
        credits = { ...credits, reserved_points: 9, remaining_points: 51 }
        yield { type: 'delta' }
        await pending
        credits = { ...credits, reserved_points: 0, remaining_points: 60 }
        throw new Error('生成失败，已退点')
      })) { /* 运行请求 */ }
    })()
    await settle()
    expect(host.textContent).toContain('本月剩余 51 / 60 点')
    await act(async () => { finish(); await expect(task).rejects.toThrow('已退点') })
    await settle()
    expect(host.textContent).toContain('本月剩余 60 / 60 点')
  } finally { await act(async () => root.unmount()); client.clear() }
})

it('点数不足仍可查看历史财务报告，禁止重新生成并展示原因', async () => {
  const credits = creditsFixture({ used_points: 56, remaining_points: 4 })
  credits.features.financial_analysis.available = false
  credits.features.financial_analysis.reason = '本月 AI 点数不足'
  vi.stubGlobal('fetch', vi.fn(async () => Response.json(credits)))
  reports.find.mockResolvedValue({ id: 'saved-report', created_at: '2026-10-19T18:00:00+08:00', focus: '' })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const host = document.createElement('div'); const root = createRoot(host)
  try {
    await act(async () => root.render(<AuthContext.Provider value={guestUser}><QueryClientProvider client={client}><StockFinancialDetail symbol="600000.SH" name="浦发银行" /></QueryClientProvider></AuthContext.Provider>))
    await settle()
    expect(host.textContent).toContain('本月 AI 点数不足')
    await act(async () => [...host.querySelectorAll('button')].find(button => button.textContent?.startsWith('AI 财务分析'))!.click())
    await settle()
    const redo = [...host.querySelectorAll('button')].find(button => button.textContent?.includes('重新分析 ·'))!
    expect(redo.disabled).toBe(true)
    await act(async () => [...host.querySelectorAll('button')].find(button => button.textContent === '查看历史报告')!.click())
    expect(reports.open).toHaveBeenCalledWith('saved-report')
    expect(reports.start).not.toHaveBeenCalled()
  } finally { await act(async () => root.unmount()); client.clear() }
})

it('取消后的首次刷新仍有预留时共享短轮询，退点完成后自动更新余额并停止短轮询', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-20T10:00:00+08:00'))
  let credits = creditsFixture({ reserved_points: 3, remaining_points: 57 })
  const fetch = vi.fn(async () => Response.json(credits))
  vi.stubGlobal('fetch', fetch)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const host = document.createElement('div'); const root = createRoot(host)
  const flush = async () => { for (let index = 0; index < 5; index++) await act(async () => { await vi.advanceTimersByTimeAsync(1) }) }
  try {
    await act(async () => root.render(<AuthContext.Provider value={guestUser}><QueryClientProvider client={client}><Probe /><AiCreditHint feature="assistant" /></QueryClientProvider></AuthContext.Provider>))
    await flush()
    expect(fetch).toHaveBeenCalledOnce()
    expect(host.textContent).toContain('本月剩余 57 / 60 点')
    // 服务端取消清理稍后完成，没有新的流事件或窗口焦点事件。
    credits = creditsFixture()
    await act(async () => { await vi.advanceTimersByTimeAsync(1_990) })
    expect(fetch).toHaveBeenCalledOnce()
    await act(async () => { await vi.advanceTimersByTimeAsync(20) })
    await flush()
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(host.textContent).toContain('本月剩余 60 / 60 点')
    await act(async () => { await vi.advanceTimersByTimeAsync(6_000) })
    expect(fetch).toHaveBeenCalledTimes(2)
  } finally { await act(async () => root.unmount()); client.clear() }
})
