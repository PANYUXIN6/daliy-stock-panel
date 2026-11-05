import type { SentimentObservations } from '@/lib/api'

// 每条轴独立表示历史分位；缺项不填零，也不合成情绪分。
const AXES = ['up_pct', 'above_ma20_pct', 'total_amount', 'index_position_60', 'limit_up_pct', 'seal_rate']
export function SentimentRadar({ data }: { data?: SentimentObservations | null }) {
  const items = AXES.map(key => data?.observations.find(o => o.key === key))
  const valid = data?.version === 'observations-v1' && items.every(o => o?.percentile != null)
  if (!valid) {
    const ready = items.filter(o => o?.percentile != null).length
    const counts = items.filter(o => o != null).map(o => o!.history_sample_count)
    const min = counts.length ? Math.min(...counts) : 0
    const max = counts.length ? Math.max(...counts) : 0
    const reason = !data || data.version !== 'observations-v1' ? '尚无可用的统一口径数据。'
      : !data.coverage.method_current ? '历史口径尚未更新，需重算市场环境。'
      : data.coverage.is_complete_day === false ? '当日收盘口径尚未确认，需刷新正式日线并重算市场环境。'
      : `六项中${ready}项已有完整500日分位；各轴有效历史${min === max ? min : `${min}–${max}`}日。`
    return <div className="space-y-1 rounded bg-elevated/40 p-2 text-[11px] leading-4 text-muted">
      <p>雷达暂不可计算：{reason}</p>
      <p>下方显示各项历史进度与缺数原因。历史不足时，可在数据页向前扩展股票及指数日线，再重算市场环境。</p>
    </div>
  }
  const point = (i: number, r: number) => [150 + Math.cos(-Math.PI / 2 + i * Math.PI / 3) * r, 115 + Math.sin(-Math.PI / 2 + i * Math.PI / 3) * r]
  const polygon = (scale: number) => items.map((_, i) => point(i, scale * 70).join(',')).join(' ')
  return <svg viewBox="0 0 300 230" className="mx-auto h-44 w-full" role="img" aria-label="六项独立历史分位雷达，不合成总分">
    {[1, .5].map(scale => <polygon key={scale} points={polygon(scale)} fill="none" stroke="hsl(var(--border))" />)}
    {items.map((o, i) => { const [x, y] = point(i, 70); const [tx, ty] = point(i, 100); return <g key={o!.key}><line x1="150" y1="115" x2={x} y2={y} stroke="hsl(var(--border))" /><text x={tx} y={ty} textAnchor="middle" className="fill-muted text-[9px]">{o!.label}<tspan x={tx} dy="12">P{o!.percentile!.toFixed(0)}</tspan></text></g> })}
    <polygon points={items.map((o, i) => point(i, o!.percentile! * .7).join(',')).join(' ')} fill="hsl(var(--accent) / .15)" stroke="hsl(var(--accent))" strokeWidth="1.5" />
  </svg>
}
