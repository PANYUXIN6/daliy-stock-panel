// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ stockList: vi.fn(), financialList: vi.fn() }))
vi.mock('./api', () => ({ api: {
  async *stockAnalyzeStream() { yield { type: 'delta', content: '未完成的个股正文' } },
  async *financialAnalyzeStream() { yield { type: 'delta', content: '未完成的财务正文' } },
  stockAnalysisReportsList: mocks.stockList,
  financialReportsList: mocks.financialList,
}, friendlyStreamError: (message: string) => message }))
afterEach(() => vi.resetModules())

it.each(['financial', 'stock'] as const)('%s 无 done 的 EOF 保留残文为失败，释放名额并允许重试', async kind => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const store = kind === 'financial' ? await import('./aiReportStore') : await import('./stockAnalysisStore')
  let current: ReturnType<typeof store.useDialogTask> | undefined
  function Probe() { current = store.useDialogTask(); return null }
  const host = document.createElement('div')
  const root = createRoot(host)
  try {
    await act(async () => root.render(<Probe />))
    let first: Awaited<ReturnType<typeof store.startAnalysis>> | undefined
    await act(async () => { first = await store.startAnalysis('600000.SH', 'A') })
    expect(current?.task).toMatchObject({ phase: 'error', content: expect.stringContaining('正文'), error: expect.stringContaining('中断') })
    let repeated: Awaited<ReturnType<typeof store.startAnalysis>> | undefined
    await act(async () => { repeated = await store.startAnalysis('600000.SH', 'A') })
    expect(repeated?.id).toBeTruthy()
    expect(repeated?.id).not.toBe(first?.id)
    for (const symbol of ['600519.SH', '300750.SZ', '000001.SZ']) {
      await act(async () => {
        const result = await store.startAnalysis(symbol, symbol)
        expect(result.error).toBeUndefined()
        expect(result.id).toBeTruthy()
      })
    }
    expect(mocks.stockList).not.toHaveBeenCalled()
    expect(mocks.financialList).not.toHaveBeenCalled()
  } finally { await act(async () => root.unmount()) }
})
