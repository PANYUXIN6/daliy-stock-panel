import type { EveningObservation, EveningPerformance, EveningPersonalRecord, EveningVariable } from '@/lib/api'
import { fmtPct, fmtPrice, fmtVolume, priceColorClass } from '@/lib/format'
import { boardLabels, dipLabel, roleLabels } from './EveningFilters'

const variableNames: Record<string, string> = {
  raw_close: '报告日收盘价', amount_day: '当日成交额', amount_median_20d: '20日成交额中位数',
  change_pct: '当日涨跌幅', return_3d: '3日涨幅', return_5d: '5日涨幅', return_10d: '10日涨幅', return_20d: '20日涨幅',
  identity_score: '辨识度', space_score: '空间分', high_proximity: '距60日高点比例', acceleration_5_20: '5/20日加速比',
  limit_up_count_20d: '20日涨停次数', consecutive_limit_ups: '连续涨停', ma5: '5日均线', ma10: '10日均线', bull_bear_line: '多空线',
  distance_ma5: '距5日均线', distance_ma10: '距10日均线', distance_bull_bear: '距多空线',
  volume_ratio_2_10: '2/10日量比', daily_amplitude: '日振幅', amplitude_ratio_2_10: '2/10日振幅比',
  rising_lows_3d: '三日低点抬高', intraday_drawdown: '日内高点回撤', first_wave_pullback: '首波回调',
  current_wave_index: '当前主升段', completed_wave_count: '已确认高点段数', first_impulse_return: '首段涨幅',
  retrace_pct: '回撤占首段涨幅比例', provisional_retrace_pct: '候选回撤比例', pivot_density: '20日摆动密度',
  first_pullback_active: '首波观察窗口有效', atr14_pct: '14日ATR占收盘比',
  low_vs_previous: '较前一交易日低点', pullback_volume_mean: '回调均量', impulse_volume_mean: '上涨段均量',
  pullback_volume_ratio: '回调与上涨段均量比', close_vs_open: '收盘相对开盘涨幅', relative_lead_score: '综合领先分',
  severe_event: '严重异动事件', near_severe_event: '即将严重异动',
}
const dipCheckLabels: Record<string, string> = {
  ...variableNames,
  environment: '市场环境', identity: '低吸地位', amount: '推荐日成交额',
  ge2: '二板及以上家数', first_pullback: '首波回调资格',
}
const dipCheckStatus = { passed: '通过', failed: '未通过', unknown: '待核验', not_applicable: '不适用' }

function variableValue(key: string, value: unknown, unit?: string) {
  if (value === null || value === undefined) return '待核验'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value !== 'number' || !Number.isFinite(value)) return String(value)
  if (key.startsWith('return_') || key.startsWith('distance_') || ['change_pct', 'high_proximity', 'daily_amplitude', 'intraday_drawdown', 'first_wave_pullback', 'first_impulse_return', 'retrace_pct', 'provisional_retrace_pct', 'atr14_pct', 'low_vs_previous', 'close_vs_open'].includes(key)) return fmtPct(value)
  if (key.startsWith('amount') || key === 'start_amount') return `${fmtVolume(value)}元`
  if (unit === '元') return `${fmtPrice(value)}元`
  return `${Number(value.toFixed(3))}${unit === '分' || unit === '次' ? unit : ''}`
}

export function EveningAllocationRank({ observation, className = 'mt-1 text-xs text-secondary' }: {
  observation?: EveningObservation | null; className?: string
}) {
  const decision = observation?.decision
  if (decision?.rank_within_theme_role == null || decision.quota_limit == null) return null
  return <p className={className}>分配前第{decision.rank_within_theme_role}名（{decision.quota_limit}个名额）</p>
}

export function EveningObservationDetails({ observation, definitions = {} }: { observation?: EveningObservation | null; definitions?: Record<string, EveningVariable> }) {
  if (!observation) return <p className="text-sm text-muted">缺少当日观察明细。</p>
  const gaps = observation.price_gaps
  const abnormal = observation.abnormal_move
  const wave = observation.wave
  const points = [['首段起点', wave?.first_wave_start], ['首段高点', wave?.first_wave_high], ['首段回调低点', wave?.first_pullback_low]] as const
  const phaseLabels = { pullback_ge_5pct: '回撤达到5%', recovered_lt_5pct: '当前回撤不足5%', window_closed: '首波回调窗口已关闭' }
  const closeLabels = { closed_by_start: '第二段量价启动', closed_by_new_high: '第二段收盘创新高', closed_by_stale: '候选极值过期' }
  const waveStatuses = { known: '结构已核验', warming_up: '等待首组摆动点确认', data_gap: '存在日线缺口', rebuilding: '结构连续性待恢复' }
  const variables = Object.entries({ ...observation.variables, ...observation.values }).map(([key]) => {
    const variable = { ...definitions[key], ...observation.variables[key] }
    const value = key in observation.values ? observation.values[key] : variable.value
    return { key, variable, value }
  })
  return <div className="space-y-4 text-sm leading-6">
    <div className="flex flex-wrap gap-x-5 gap-y-1 text-secondary"><span>{roleLabels[observation.role ?? ''] ?? '角色待核验'}{observation.role_status === 'review_pending' ? ' · 跟踪待复核' : ''}</span><span>{boardLabels[observation.board ?? ''] ?? '交易板块待核验'}</span><span>低吸：{dipLabel(observation.dip)}</span></div>
    <p>{observation.reason}</p>
    <EveningAllocationRank observation={observation} className="text-secondary" />
    {observation.role_status === 'review_pending' && observation.role_by_history_tracking && <p className="text-secondary">历史身份 {roleLabels[observation.role_by_history_tracking]} · 待复核</p>}
    {wave ? <div className="rounded-md border border-border p-3">
      <h4 className="font-medium">波次与首波回调</h4>
      <p className="text-secondary">{waveStatuses[wave.data_status]}{wave.missing_from && ` · 缺口始于 ${wave.missing_from}`}</p>
      {wave.pullback_phase_at_time && <p className="text-secondary">{phaseLabels[wave.pullback_phase_at_time]}{wave.window_close_reason_codes.length > 0 && ` · ${wave.window_close_reason_codes.map(code => closeLabels[code]).join('、')}`}</p>}
      <ul className="text-secondary">{points.map(([label, point]) => point && <li key={point.pivot_id}><span className="whitespace-nowrap">{label} {fmtPrice(point.price)}</span>{' · '}
        <span className="whitespace-nowrap">极值日 {point.extreme_date}</span>{' · '}
        <span className="whitespace-nowrap">确认日 {point.confirmed_at}</span></li>)}</ul>
      {wave.pullback_volume && <p className="text-secondary">回调缩量按 {wave.pullback_volume.start_exclusive} 之后至 {wave.pullback_volume.end} 的 {wave.pullback_volume.sessions} 个成交日计算。</p>}
      {wave.provisional_extreme && <p className="text-secondary">候选{wave.provisional_extreme.kind === 'H' ? '高点' : '低点'} {fmtPrice(wave.provisional_extreme.price)} · {wave.provisional_extreme.extreme_date} · 静置 {wave.provisional_extreme.idle_sessions} 日</p>}
    </div> : <p className="text-muted">{observation.dip.rule_version ? '波次资料待核验' : '波次详情当时未记录'}</p>}
    {!!observation.related_themes.length && <p className="text-secondary">关联题材：{observation.related_themes.map(theme => theme.name).join('、')}</p>}
    {!!observation.dip.reasons.length && <div className="rounded-md bg-warning/5 p-3 text-warning"><p className="font-medium">低吸依据</p><ul className="mt-1 list-inside list-disc">{observation.dip.reasons.map(reason => <li key={reason}>{reason}</li>)}</ul></div>}
    {observation.dip.checks && <details className="rounded-md border border-border px-3 py-2">
      <summary className="cursor-pointer font-medium">五道门与形态明细</summary>
      <div className="mt-2 grid gap-3 sm:grid-cols-3">
        {([
          { label: '共同资格', checks: observation.dip.checks },
          { label: 'E1 · 异动前形态', ...observation.dip.e1 },
          { label: 'E2 · 异动后形态', ...observation.dip.e2 },
        ]).map(group => <div key={group.label} className="min-w-0 text-xs">
          <p className="mb-1 font-medium">{group.label}</p>
          {'event' in group && group.event?.title && <p className="text-secondary">{group.event.disclosed_on} · {group.event.title}</p>}
          {'event' in group && group.event?.disclosure_days && <p className="text-secondary">同向异动 {group.event.count} 次，严重异动门槛 {group.event.threshold} 次。上榜日 {group.event.disclosure_days.join('、')}。</p>}
          {'event_applicable' in group && <p className="text-muted">事件依据{group.event_applicable === true ? '已核验' : group.event_applicable === false ? '不适用' : '待核验'}</p>}
          <dl>{group.checks?.map(check => <div className="flex justify-between gap-2" key={check.check_id}>
            <dt className="text-secondary">{dipCheckLabels[check.check_id] ?? check.check_id}</dt>
            <dd className="shrink-0">{dipCheckStatus[check.status]}</dd>
          </div>)}</dl>
        </div>)}
      </div>
    </details>}
    <div className="grid gap-4 md:grid-cols-2">
      <div><h4 className="font-medium">未回补价格缺口</h4>{gaps?.status === 'known' ? gaps.intervals.length ? <ul className="text-secondary">{gaps.intervals.map((gap, index) => <li key={`${gap.date}:${index}`}>{gap.date} · {gap.direction === 'up' ? '向上' : gap.direction === 'down' ? '向下' : gap.direction} · {fmtPrice(gap.lower)}—{fmtPrice(gap.upper)}</li>)}</ul> : <p className="text-secondary">截止 {gaps.as_of} 未发现未回补缺口。</p> : <p className="text-muted">{gaps?.reason ?? '缺口位置待核验'}</p>}</div>
      <div><h4 className="font-medium">异常波动接近度 · 近似计算</h4>{abnormal?.status === 'approximate' && <ul className="text-secondary">{Object.entries(abnormal.windows ?? {}).map(([window, evidence]) => <li key={window}>{window}日 · 偏离 {fmtPct(evidence.value)} · 接近度 {evidence.closeness == null ? '未知' : `${(evidence.closeness * 100).toFixed(1)}%`}</li>)}</ul>}<p className="text-muted">{abnormal?.limitation ?? '偏离度为估算值，适用事件见上方 E1／E2 明细。'}</p></div>
    </div>
    <details className="rounded-md border border-border"><summary className="cursor-pointer px-3 py-2 font-medium">变量值与计算依据</summary><dl className="grid gap-3 border-t border-border p-3 sm:grid-cols-2 xl:grid-cols-3">{variables.map(({ key, variable, value }) => <div key={key} className="min-w-0 rounded bg-elevated/40 p-3 text-xs"><dt className="font-medium text-foreground">{variableNames[key] ?? key}<span className="float-right ml-2 font-mono">{variableValue(key, value, variable.unit)}</span></dt><dd className="mt-2 break-words text-secondary">{variable.formula}</dd><dd className="text-muted">{variable.price_basis} · {variable.as_of ?? '时点未知'}</dd>{variable.missing_reason && <dd className="text-warning">{variable.missing_reason}</dd>}</div>)}</dl></details>
  </div>
}

export function eveningPerformanceLabel(value?: EveningPerformance) {
  switch (value?.status) {
    case 'evaluated': return fmtPct(value.return)
    case 'overdue_pending_review': return '已到期，待回看'
    case 'unavailable': return '不可评价'
    default: return value?.target_date ? '未到期' : '目标日待确认'
  }
}

export function EveningPerformanceDetails({ record }: { record: EveningPersonalRecord }) {
  return <div className="space-y-2 text-xs"><h4 className="font-medium text-sm">推荐后价格表现</h4><p className="text-muted">以推荐日收盘为观察基准，不代表可成交收益，也不等于你的盈亏记录。</p><div className="grid gap-3 sm:grid-cols-3">{(['1', '3', '5'] as const).map(window => {
    const value = record.performance[window]
    const separateDates = value?.path?.as_of && value.path.as_of !== value.as_of
    return <div key={window} className="rounded-lg border border-border p-3"><p className="font-medium">D+{window} · {value?.target_date ?? '目标交易日待确认'}</p><p className={`mt-2 font-mono text-lg ${priceColorClass(value?.return)}`}>{eveningPerformanceLabel(value)}</p>{value?.reason && <p className="mt-1 text-muted">{value.reason}</p>}{!!value?.trading_constraints?.length && <p className="mt-1 text-warning">成交限制：{value.trading_constraints.join('、')}</p>}{value?.path && <p className="mt-1 text-muted">{value.path.status === 'evaluated'
      ? `期间最好 ${fmtPct(value.path.max_favorable_return)} · 最差 ${fmtPct(value.path.max_adverse_return)}`
      : '期间路径资料不足'} · 有效 {value.path.valid_count}/{value.path.expected_count} 日
      {value.path.suspended_count > 0 && ` · 停牌 ${value.path.suspended_count} 日`}{separateDates && ` · 期间路径截至 ${value.path.as_of}`}</p>}{value?.as_of && <p className="mt-1 text-muted">{separateDates ? '端点收益截至' : '评价截至'} {value.as_of}</p>}</div>
  })}</div></div>
}
