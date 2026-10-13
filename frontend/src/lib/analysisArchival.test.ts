import { expect, it, vi } from 'vitest'
import { startAnalysis as startStock, findLatestHistoryReport as stockHistory } from './stockAnalysisStore'
import { startAnalysis as startFinancial, findLatestHistoryReport as financialHistory } from './aiReportStore'
const mocks = vi.hoisted(() => ({ stockSave: vi.fn(), financialSave: vi.fn(), stockList: vi.fn(), financialList: vi.fn() }))
vi.mock('./api', () => ({ api: {
  async *stockAnalyzeStream() { yield { type: 'delta', content: '个股报告' }; yield { type: 'done', archived: true, report_id: 'stock-report' } },
  async *financialAnalyzeStream() { yield { type: 'delta', content: '财务报告' }; yield { type: 'done', archived: true, report_id: 'financial-report' } },
  stockAnalysisReportSave: mocks.stockSave, financialReportSave: mocks.financialSave,
  stockAnalysisReportsList: mocks.stockList, financialReportsList: mocks.financialList,
}, friendlyStreamError: (s: string) => s }))
it('uses collector archived stock and financial reports without duplicate writes', async () => {
  mocks.stockList.mockResolvedValue({ reports: [{ id: 'stock-report', symbol: '600000.SH', content: '个股报告' }] })
  mocks.financialList.mockResolvedValue({ reports: [{ id: 'financial-report', symbol: '600000.SH', content: '财务报告' }] })
  await startStock('600000.SH', '浦发银行')
  await startFinancial('600000.SH', '浦发银行')
  await vi.waitFor(() => { expect(mocks.stockList).toHaveBeenCalled(); expect(mocks.financialList).toHaveBeenCalled() })
  expect((await stockHistory('600000.SH'))?.id).toBe('stock-report')
  expect((await financialHistory('600000.SH'))?.id).toBe('financial-report')
  expect(mocks.stockSave).not.toHaveBeenCalled()
  expect(mocks.financialSave).not.toHaveBeenCalled()
})
