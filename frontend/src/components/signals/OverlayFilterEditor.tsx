import { useQuery } from '@tanstack/react-query'
import { api, type CustomSignalCondition } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { ConditionEditor } from './ConditionEditor'

export function OverlayFilterEditor({ conditions, onChange, backend }: {
  conditions: CustomSignalCondition[]
  onChange: (conditions: CustomSignalCondition[]) => void
  backend?: string
}) {
  const supported = backend !== 'composite' && backend !== 'minute_filter'
  const options = useQuery({ queryKey: QK.customSignalsOptions, queryFn: api.customSignalsOptions, enabled: supported })
  if (!supported) return <p className="text-xs text-muted">叠加条件适用于日线策略；组合策略请在子策略中配置。</p>
  if (options.isPending) return <p className="text-xs text-muted">正在加载条件字段…</p>
  if (options.isError || !options.data) return <div role="alert" className="text-xs text-danger">条件字段加载失败 <button type="button" onClick={() => options.refetch()} className="underline">重试</button></div>
  const stringFields = options.data.stringFields ?? []
  const numericOnly = backend === 'matrix_native'
  const fields = options.data.fields.filter(f => !numericOnly || !stringFields.includes(f.key))
  const groups = options.data.groups?.map(g => ({ ...g, fields: g.fields.filter(f => !numericOnly || !stringFields.includes(f.key)) })).filter(g => g.fields.length)
  return <div className="space-y-2">
    <p className="text-[11px] text-muted">全部条件满足才可入选；先过滤再评分，不影响持仓卖出。前 N 日按该标的交易日回看，扩展字段须有按日历史数据。</p>
    <ConditionEditor conditions={conditions} onChange={onChange} options={{ ...options.data, fields, groups, stringFields: numericOnly ? [] : stringFields }} allowEmpty />
  </div>
}
