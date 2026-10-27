export const CLASSIFICATIONS: Record<string, string> = {
  countertrend_up: '逆势上涨', resilient: '相对抗跌', flat_in_decline: '调整中持平',
  lagging_in_decline: '调整中未跑赢', relative_leader: '相对领涨', lagging_in_rise: '上涨环境未跑赢',
  benchmark_flat: '基准区间持平', pending: '待评估',
}
export const REASONS: Record<string, string> = {
  missing_quote: '正式行情缺失', non_formal_snapshot: '仅有快照', halted: '明确停牌',
  invalid_quote: '价格或成交字段异常', adjusted_price_unavailable: '复权价格缺失或不一致',
  invalid_amount: '成交额缺失或异常',
  insufficient_formation_history: '形成日前历史不足', not_listed: '形成时尚未上市',
  unknown_asset: '证券维表未识别为A股', previous_close_missing: '前一交易日价格缺失',
}
export const OBSERVATION_STATES: Record<string, string> = {
  ready: '完整', partial: '部分证据缺失', data_missing: '正式行情不足', future: '尚未到来',
  not_yet_observed: '超出观察截至日', calendar_missing: '日历尚未覆盖',
}
export const pct = (v: number | null | undefined) => v == null ? '—' : `${(v * 100).toFixed(2)}%`
export const pp = (v: number | null | undefined) => v == null ? '—' : `${(v * 100).toFixed(2)}个百分点`
export const ratio = (v: number | null | undefined) => v == null ? '—' : `${v.toFixed(2)}倍`
export const amount = (v: number | null | undefined) => v == null ? '—' : v >= 1e8 ? `${(v / 1e8).toFixed(2)}亿` : `${(v / 1e4).toFixed(2)}万`
export const truth = (v: boolean | null | undefined) => v == null ? '未知' : v ? '满足' : '不满足'
export const reasonCounts = (values: Record<string, number>) => Object.entries(values).map(([k, n]) => `${REASONS[k] ?? k} ${n}`).join('；') || '无'
