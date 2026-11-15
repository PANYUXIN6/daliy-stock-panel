import { MARKET_PHASE_LABELS, type MarketCohortPerformance, type MarketPhase, type MarketSentimentClassification } from '@/lib/api'

const STATE_LABELS: Record<MarketSentimentClassification['sentiment_state'], string> = {
  hot_fragile: '高热但脆弱', hot: '高热良好', warm: '高热分歧', cold: '冷清偏弱',
  repair: '修复', mixed: '常态／混合', unknown: '综合判断待核验',
}
const HEAT_LABELS = { low: '低', medium: '中', high: '高', unknown: '数据不足' }
const QUALITY_LABELS = { good: '良好', divergent: '分歧', fragile: '脆弱', unknown: '数据不足' }
const DIRECTION_LABELS = { improving: '正在改善', deteriorating: '正在恶化', mixed: '震荡／分歧', unknown: '方向数据不足' }
const pct = (value: number | null) => value == null ? '—' : `${(value * 100).toFixed(1)}%`

function CohortEvidence({ label, sample }: { label: string; sample: MarketCohortPerformance }) {
  return <p className="break-words leading-5">
    {label} <span className="font-mono text-foreground">{pct(sample.value)}</span>
    {' · '}有效 {sample.valid ?? '—'} / 原集合 {sample.pool ?? '—'}
    {' · '}停牌 {sample.suspended ?? '—'}，缺数 {sample.missing ?? '—'}，有成交限制 {sample.limited ?? '—'}
    {sample.missing_reason && <span className="text-warning"> · {sample.missing_reason}</span>}
  </p>
}

/** 同一共用判定的只读呈现；晚报保存当时结果，环境页读取相同日期结果。 */
export function SentimentClassification({ data, compact = false }: { data?: MarketSentimentClassification | null; compact?: boolean }) {
  if (!data) return <p className="text-xs text-muted">旧版未记录综合情绪。</p>
  const evidence = data.evidence
  const tone = ['hot_fragile', 'cold'].includes(data.sentiment_state) ? 'text-warning' : 'text-foreground'
  return <section className="min-w-0 rounded-lg border border-border bg-surface/40 px-3 py-2.5 text-xs" aria-label="市场情绪四维">
    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
      <p className="font-medium"><span className={tone}>{STATE_LABELS[data.sentiment_state]}</span><span className="ml-2 font-normal text-secondary">{DIRECTION_LABELS[data.sentiment_direction]}</span></p>
      <span className="font-mono text-[10px] text-muted">{data.as_of}</span>
    </div>
    <dl className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-secondary">
      <div className="flex gap-1.5"><dt className="text-muted">温度</dt><dd>{HEAT_LABELS[data.heat_level]}</dd></div>
      <div className="flex gap-1.5"><dt className="text-muted">封板质量</dt><dd>{QUALITY_LABELS[data.quality_level]}</dd></div>
      <div className="flex gap-1.5"><dt className="text-muted">周期阶段</dt><dd>{MARKET_PHASE_LABELS[data.phase as MarketPhase] ?? '数据不足'}</dd></div>
    </dl>
    <p className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-muted">
      <span>涨停 <span className="font-mono text-secondary">{evidence.limit_up ?? '—'}</span> 家</span>
      <span>炸板率 <span className="font-mono text-secondary">{pct(evidence.broken_rate)}</span></span>
      <span>昨日涨停溢价 <span className="font-mono text-secondary">{pct(evidence.previous_limit_up_premium.value)}</span>{!evidence.previous_limit_up_premium.usable && '（待核验）'}</span>
      {!compact && <span>晋级率 <span className="font-mono text-secondary">{pct(evidence.promo_rate)}</span> / {evidence.promo_pool ?? '—'} 家</span>}
    </p>
    <details className="mt-2 border-t border-border/60 pt-2 text-[11px] text-muted">
      <summary className="w-fit cursor-pointer rounded-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary">判定依据与样本</summary>
      <div className="mt-2 space-y-1">
        <CohortEvidence label="昨日涨停集合" sample={evidence.previous_limit_up_premium} />
        <CohortEvidence label="昨日炸板集合" sample={evidence.previous_broken_performance} />
        <p>方向比较此前5个完整交易日的均值，不含当日；至少两项同向，且三项证据完整时才确认方向。</p>
        <p>集合均值为同口径复权收盘价格表现，停牌不顺延，不代表实际可成交收益。</p>
        {data.unknown_reasons.map(reason => <p key={reason} className="text-warning">{reason}</p>)}
      </div>
    </details>
  </section>
}
