import type { EveningObservation, EveningPersonalRecord } from '@/lib/api'
import { fmtPct, fmtPrice, fmtVolume, priceColorClass } from '@/lib/format'
import { boardLabels, dipLabels, roleLabels } from './EveningFilters'

const variableNames: Record<string, string> = {
  raw_close: '报告日收盘价', amount_day: '当日成交额', amount_median_20d: '20日成交额中位数',
  change_pct: '当日涨跌幅', return_3d: '3日涨幅', return_5d: '5日涨幅', return_10d: '10日涨幅', return_20d: '20日涨幅',
  identity_score: '辨识度', space_score: '空间分', high_proximity: '距60日高点比例', acceleration_5_20: '5/20日加速比',
  limit_up_count_20d: '20日涨停次数', consecutive_limit_ups: '连续涨停', ma5: '5日均线', ma10: '10日均线', bull_bear_line: '多空线',
  distance_ma5: '距5日均线', distance_ma10: '距10日均线', distance_bull_bear: '距多空线',
  volume_ratio_2_10: '2/10日量比', daily_amplitude: '日振幅', amplitude_ratio_2_10: '2/10日振幅比',
  rising_lows_3d: '三日低点抬高', intraday_drawdown: '日内高点回撤', first_wave_pullback: '首波回调',
  wave_index: '主升波次', first_pullback: '首波首次回调', severe_event: '严重异动事件', near_severe_event: '即将严重异动',
}
function variableValue(key: string, value: unknown, unit?: string) {
  if (value === null || value === undefined) return '待核验'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value !== 'number' || !Number.isFinite(value)) return String(value)
  if (key.startsWith('return_') || key.startsWith('distance_') || ['change_pct', 'high_proximity', 'daily_amplitude', 'intraday_drawdown', 'first_wave_pullback'].includes(key)) return fmtPct(value)
  if (key.startsWith('amount')) return `${fmtVolume(value)}元`
  if (unit === '元') return `${fmtPrice(value)}元`
  return `${Number(value.toFixed(3))}${unit === '分' || unit === '次' ? unit : ''}`
}

export function EveningObservationDetails({ observation }: { observation?: EveningObservation | null }) {
  if (!observation) return <p className="text-sm text-muted">角色、低吸和变量明细：旧版未记录。</p>
  const gaps = observation.price_gaps
  const abnormal = observation.abnormal_move
  return <div className="space-y-4 text-sm leading-6">
    <div className="flex flex-wrap gap-x-5 gap-y-1 text-secondary"><span>{roleLabels[observation.role ?? ''] ?? '角色待核验'}{observation.role_status === 'review_pending' ? ' · 跟踪待复核' : ''}</span><span>{boardLabels[observation.board ?? ''] ?? '交易板块待核验'}</span><span>低吸：{dipLabels[observation.dip.status] ?? '待核验'}</span></div>
    <p>{observation.reason}</p>
    {!!observation.related_themes.length && <p className="text-secondary">关联题材：{observation.related_themes.map(theme => theme.name).join('、')}</p>}
    {!!observation.dip.reasons.length && <div className="rounded-md bg-warning/5 p-3 text-warning"><p className="font-medium">低吸条件与待核验项</p><ul className="mt-1 list-inside list-disc">{observation.dip.reasons.map(reason => <li key={reason}>{reason}</li>)}</ul></div>}
    <div className="grid gap-4 md:grid-cols-2">
      <div><h4 className="font-medium">未回补价格缺口</h4>{gaps?.status === 'known' ? gaps.intervals.length ? <ul className="text-secondary">{gaps.intervals.map((gap, index) => <li key={`${gap.date}:${index}`}>{gap.date} · {gap.direction === 'up' ? '向上' : gap.direction === 'down' ? '向下' : gap.direction} · {fmtPrice(gap.lower)}—{fmtPrice(gap.upper)}</li>)}</ul> : <p className="text-secondary">截止 {gaps.as_of} 未发现未回补缺口。</p> : <p className="text-muted">{gaps?.reason ?? '缺口位置待核验'}</p>}</div>
      <div><h4 className="font-medium">异常波动接近度 · 近似计算</h4>{abnormal?.status === 'approximate' && <ul className="text-secondary">{Object.entries(abnormal.windows ?? {}).map(([window, evidence]) => <li key={window}>{window}日 · 偏离 {fmtPct(evidence.value)} · 接近度 {evidence.closeness == null ? '未知' : `${(evidence.closeness * 100).toFixed(1)}%`}</li>)}</ul>}<p className="text-muted">{abnormal?.limitation ?? '事件级严重异动尚未核验，不能视为E1/E2前置已确认。'}</p></div>
    </div>
    <details className="rounded-md border border-border"><summary className="cursor-pointer px-3 py-2 font-medium">变量值与计算依据</summary><dl className="grid gap-3 border-t border-border p-3 sm:grid-cols-2 xl:grid-cols-3">{Object.entries(observation.variables).map(([key, variable]) => <div key={key} className="min-w-0 rounded bg-elevated/40 p-3 text-xs"><dt className="font-medium text-foreground">{variableNames[key] ?? key}<span className="float-right ml-2 font-mono">{variableValue(key, variable.value, variable.unit)}</span></dt><dd className="mt-2 break-words text-secondary">{variable.formula}</dd><dd className="text-muted">{variable.price_basis} · {variable.as_of ?? '时点未知'}</dd>{variable.missing_reason && <dd className="text-warning">{variable.missing_reason}</dd>}</div>)}</dl></details>
  </div>
}

export function EveningPerformanceDetails({ record }: { record: EveningPersonalRecord }) {
  return <div className="space-y-2 text-xs"><h4 className="font-medium text-sm">推荐后价格表现</h4><p className="text-muted">以推荐日收盘为观察基准，不代表可成交收益，也不等于你的盈亏记录。</p><div className="grid gap-3 sm:grid-cols-3">{(['1', '3', '5'] as const).map(window => {
    const value = record.performance[window]
    return <div key={window} className="rounded-lg border border-border p-3"><p className="font-medium">D+{window} · {value?.target_date ?? '目标交易日待确认'}</p><p className={`mt-2 font-mono text-lg ${priceColorClass(value?.return)}`}>{value?.status === 'evaluated' ? fmtPct(value.return) : value?.status === 'not_due' ? '未到期' : '不可评价'}</p>{value?.reason && <p className="mt-1 text-muted">{value.reason}</p>}{!!value?.trading_constraints?.length && <p className="mt-1 text-warning">成交限制：{value.trading_constraints.join('、')}</p>}{value?.as_of && <p className="mt-1 text-muted">评价截至 {value.as_of}</p>}</div>
  })}</div></div>
}
