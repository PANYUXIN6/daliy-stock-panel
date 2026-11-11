import type { SentimentObservation, SentimentObservations as ObservationSet } from '@/lib/api'
import { MethodHelp } from '@/components/MethodHelp'
import { fmtBigNum } from '@/lib/format'

const GROUPS = [
  ['breadth', '市场强弱 / 广度', '参与上涨及站上均线的股票比例'],
  ['activity', '成交活跃度', '成交变化，不代表乐观或悲观'],
  ['price', '价格位置', '仅代表上证指数的价格背景'],
  ['speculation', '短线投机', '涨跌停与连板结构，不代表全市场'],
  ['external', '补充数据', '尚无可用数据时不纳入判断'],
] as const
export function SentimentObservationHelp({ data }: { data?: ObservationSet | null }) {
  return <MethodHelp label="状态观察口径">
    <p>各维度独立观察，不合成情绪总分。收盘落盘不代表全市场覆盖，各指标分母见有效样本。</p>
    <p>分位使用观察日之前固定500个交易日，切换图表范围不改变分位。相同值按中位秩计入，常量序列为P50。高/低极端分别为分位≥90/≤10，持续天数仅描述历史。</p>
    {GROUPS.map(([key, label, description]) => <p key={key}>{label} · {description}</p>)}
    {data?.version === 'observations-v1' && <>
      <p>本地自定义状态统计，不是论文复现或收益预测。{data.universe.exclude_st == null ? 'ST过滤口径未知。' : data.universe.exclude_st ? '剔除ST。' : '包含ST。'}{data.universe.suspension_rule}。</p>
      {data.limitations.map(item => <p key={item}>{item}</p>)}
    </>}
  </MethodHelp>
}

export function observationValue(item: SentimentObservation, value = item.value, change = false): string {
  if (value == null || !Number.isFinite(value)) return '不可计算'
  const sign = change && value > 0 ? '+' : ''
  if (item.key === 'max_consecutive') return `${sign}${value.toFixed(0)}板`
  if (item.unit === 'CNY') return sign + fmtBigNum(value)
  if (item.key === 'amount_ratio_5_20') return `${sign}${value.toFixed(2)}倍`
  if (item.unit === 'ratio') return `${sign}${(value * 100).toFixed(1)}${change ? '个百分点' : '%'}`
  if (item.unit === '%') return `${sign}${value.toFixed(1)}${change ? '个百分点' : '%'}`
  return `${sign}${value.toFixed(item.unit === 'count' ? 0 : 1)}${item.unit === 'count' ? '家' : ''}`
}
export function SentimentObservations({ data, expectedDate, compact = false }: { data?: ObservationSet | null; expectedDate?: string; compact?: boolean }) {
  if (!data || data.version !== 'observations-v1') return <p className="p-3 text-xs text-muted">状态观察不可计算：尚无可用的统一口径数据。</p>
  const historyLimited = data.observations.some(o => o.value != null && o.percentile == null
    && (o.history_sample_count < 500 || data.coverage.is_complete_day !== false))
  return <div className="min-w-0 space-y-3 text-xs">
    <div className="flex flex-wrap gap-x-3 gap-y-1 text-muted">
      {data.coverage.is_complete_day === false && <span className="text-warning">收盘口径尚未确认，分位不可计算</span>}
      {expectedDate && expectedDate !== data.as_of && <span role="status" className="text-warning">日期不同：页面 {expectedDate}</span>}
      {!data.coverage.method_current && <span className="text-warning">历史口径尚未更新，指标不可计算</span>}
    </div>
    {compact && <p className="text-secondary">{data.context.join('；') || '暂无可用状态摘要'}。</p>}
    {GROUPS.map(([key, label]) => {
      const allItems = data.observations.filter(o => o.group === key)
      const compactKeys = ['up_pct', 'above_ma20_pct', 'total_amount', 'amount_ratio_5_20', 'index_position_60', 'limit_up_pct', 'seal_rate']
      const items = compact ? allItems.filter(o => compactKeys.includes(o.key)) : allItems
      if (!items.length) return null
      return <section key={key}>
        <h3 className="mb-1 font-medium text-foreground">{label}</h3>
        <div className="max-w-full overflow-x-auto" tabIndex={0} role="region" aria-label={`${label}指标表`}>
          <table className={`w-full whitespace-nowrap text-left text-[11px] ${compact ? '' : 'min-w-[640px]'}`}><thead className="text-[10px] text-muted"><tr><th className="py-1 font-normal">指标</th><th className="font-normal">原值</th><th className="font-normal">历史分位</th>{!compact && <><th className="font-normal">较5日前</th><th className="font-normal">极端持续</th><th className="font-normal">有效样本</th></>}</tr></thead>
            <tbody>{items.map(o => <tr key={o.key} className="border-t border-border/40">
              <td className="py-1 pr-2">{o.label}</td><td className="pr-2 font-mono">{o.value == null ? <span className="font-sans text-muted" title={o.missing_reason ?? '尚无可用数据'}>尚无可用数据</span> : observationValue(o)}</td>
              <td className="pr-2 font-mono" title={`${o.percentile_reason ?? '此前500交易日'}；有效 ${o.history_sample_count} 日`}>{o.percentile == null ? <><span>—</span><span className="ml-1 text-[10px] text-muted">{o.history_sample_count}/500日</span></> : `P${o.percentile.toFixed(0)}`}</td>
              {!compact && <><td className="pr-2 font-mono">{o.change_5d == null ? '—' : observationValue(o, o.change_5d, true)}</td><td className="pr-2">{o.extreme_side ? `${o.extreme_side === 'low' ? '低' : '高'}分位 ${o.extreme_streak} 日` : '—'}</td><td>{o.denominator == null ? '—' : `${o.denominator} 家`}</td></>}
            </tr>)}</tbody>
          </table>
        </div>
        {items.filter(o => o.value == null).map(o => <p key={o.key} className="mt-1 break-words text-[10px] leading-4 text-muted">{o.label}：{o.missing_reason ?? '尚无可用数据'}</p>)}
      </section>
    })}
    {compact && <p className="text-[10px] text-muted">融资、期权、行业ETF净申购：尚无可用数据。完整分项与变化见市场环境。</p>}
    {historyLimited && <p className="text-[10px] leading-4 text-muted">部分指标历史不足或不可比，暂不提供分位（—）；原值仍独立展示。</p>}
  </div>
}
