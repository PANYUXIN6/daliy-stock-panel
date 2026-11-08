// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from './api'
import type { ColumnConfig } from './list-columns'
import { useExtSeries } from './useExtSeries'
const columns: ColumnConfig[] = [{ id: 'flow', label: '资金', visible: true, source: { type: 'ext', configId: 'flow', fieldName: 'net' }, extDisplay: { displayMode: 'text', snapshotRender: 'line', chartDays: 5 } }]
let host: HTMLDivElement, root: Root, client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div'); root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.spyOn(api, 'extDataSchemaAll').mockResolvedValue({ items: [{ id: 'flow', label: '资金', mode: 'timeseries', granularity: 'daily', columns: [] }] })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); vi.restoreAllMocks() })
function View({ cols, date }: { cols: ColumnConfig[]; date: string }) {
  const result = useExtSeries(cols, ['600001.SH', '600000.SH', '600000.SH'], date)
  return <div>{result.isLoading ? '加载' : result.isError ? '失败' : JSON.stringify(result.data ?? {})}</div>
}
async function render(cols = columns, date = '2026-11-05') {
  await act(async () => root.render(<QueryClientProvider client={client}><View cols={cols} date={date} /></QueryClientProvider>))
  for (let i = 0; i < 5; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
it('整列按唯一标的批量请求，切换日期和显示粒度不沿用旧序列', async () => {
  const series = vi.spyOn(api, 'extDataSeries').mockResolvedValue({ config_id: 'flow', field: 'net', date: null, items: { '600000.SH': [['2026-11-05', 1]] } })
  await render()
  expect(series).toHaveBeenCalledWith('flow', 'net', '2026-11-05', ['600000.SH', '600001.SH'], 5, 'daily')
  expect(host.textContent).toContain('2026-11-05')
  series.mockReturnValue(new Promise(() => {}))
  await render([{ ...columns[0], extDisplay: { ...columns[0].extDisplay!, seriesGranularity: 'intraday' } }], '2026-11-06')
  expect(series).toHaveBeenLastCalledWith('flow', 'net', '2026-11-06', ['600000.SH', '600001.SH'], 5, 'intraday')
  expect(host.textContent).toBe('加载')
})
it('未启用图表列时不读取序列，结构读取失败有明确错误状态', async () => {
  const series = vi.spyOn(api, 'extDataSeries')
  await render([{ ...columns[0], visible: false }])
  expect(api.extDataSchemaAll).not.toHaveBeenCalled()
  expect(series).not.toHaveBeenCalled()
  vi.mocked(api.extDataSchemaAll).mockRejectedValue(new Error('无法读取结构'))
  await render()
  expect(host.textContent).toBe('失败')
})
