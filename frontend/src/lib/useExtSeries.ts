import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from './api'
import { QK } from './queryKeys'
import type { ColumnConfig } from './list-columns'

export type ExtSeriesData = Record<string, Record<string, [string, number][]>>

/** 同一列表按列批量读取全部标的，缓存覆盖日期、粒度与窗口，切换期间不展示旧上下文。 */
export function useExtSeries(columns: ColumnConfig[], symbols: string[], date?: string | null) {
  const chartColumns = useMemo(() => columns.filter(column => column.visible && column.source.type === 'ext'
    && (column.extDisplay?.snapshotRender === 'bar' || column.extDisplay?.snapshotRender === 'line')), [columns])
  const schema = useQuery({ queryKey: QK.extDataSchemaAll, queryFn: api.extDataSchemaAll,
    enabled: chartColumns.length > 0, staleTime: 5 * 60_000 })
  const requests = chartColumns.flatMap(column => {
    if (column.source.type !== 'ext') return []
    const source = column.source
    const granularity = column.extDisplay?.seriesGranularity
      ?? schema.data?.items.find(table => table.id === source.configId)?.granularity ?? undefined
    return [{ id: column.id, configId: source.configId, field: source.fieldName,
      days: Math.max(1, Math.min(120, column.extDisplay?.chartDays ?? 10)), granularity }]
  })
  const columnsKey = JSON.stringify(requests)
  const symbolsKey = [...new Set(symbols)].sort().join(',')
  const series = useQuery<ExtSeriesData>({
    queryKey: QK.extSeries(columnsKey, date, symbolsKey),
    queryFn: async () => Object.fromEntries(await Promise.all(requests.map(async request => {
      const result = await api.extDataSeries(request.configId, request.field, date, symbolsKey.split(','), request.days, request.granularity)
      return [request.id, result.items]
    }))),
    enabled: requests.length > 0 && !!symbolsKey && schema.isSuccess,
    staleTime: 5 * 60_000,
  })
  return { ...series, isLoading: requests.length > 0 && !!symbolsKey && (schema.isLoading || series.isLoading),
    isError: requests.length > 0 && (schema.isError || series.isError) }
}
