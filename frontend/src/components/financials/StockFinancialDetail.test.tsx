// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, it, vi } from 'vitest'
import { StockFinancialDetail } from './StockFinancialDetail'

const state = vi.hoisted(() => ({ row: {} as Record<string, unknown> }))
vi.mock('@/lib/useFinancials', () => {
  const query = () => ({ data: { data: [state.row] }, isLoading: false })
  return {
    useFinancialMetrics: query, useFinancialIncome: query, useFinancialBalanceSheet: query,
    useFinancialCashFlow: query, useFinancialShares: query,
  }
})
vi.mock('@/lib/aiReportStore', () => ({ startAnalysis: vi.fn(), findLatestHistoryReport: vi.fn(), openHistoryReport: vi.fn() }))

it('区别更新日期与无日期指标的采集日期，估值反推 BPS 标记为估算', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  state.row = { period_end: '2025-06-30', announce_date: '2026-08-15', date_basis: 'observed',
    observed_at: '2026-10-12T16:30:00+00:00', bps: 5., bps_basis: 'valuation_snapshot' }
  const host = document.createElement('div')
  const root = createRoot(host)
  try {
    await act(async () => root.render(<StockFinancialDetail symbol="600519.SH" name="贵州茅台" />))
    expect(host.textContent).toContain('采集 2026-10-13')
    expect(host.textContent).not.toContain('披露')
    expect(host.textContent).toContain('每股净资产 BPS（估算）')
    state.row = { period_end: '2025-06-30', announce_date: '2026-08-15', date_basis: 'source_update',
      source_update_date: '2026-08-15', revenue: 100. }
    const income = [...host.querySelectorAll('button')].find(node => node.textContent === '利润表')!
    await act(async () => income.click())
    expect(host.textContent).toContain('更新 2026-08-15')
    expect(host.textContent).not.toContain('披露')
  } finally {
    await act(async () => root.unmount())
  }
})
