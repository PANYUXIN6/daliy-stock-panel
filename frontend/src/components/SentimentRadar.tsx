import type { SentimentObservations, SentimentRadarSnapshot } from '@/lib/api'

// 每条轴独立表示历史分位；缺项不填零，也不合成情绪分。
const AXES = ['up_pct', 'above_ma20_pct', 'total_amount', 'index_position_60', 'limit_up_pct', 'seal_rate']
export function SentimentRadar({ data, previous }: { data?: SentimentObservations | null; previous?: SentimentRadarSnapshot | null }) {
  const pendingClose = data?.coverage.is_complete_day === false
  const fallback = pendingClose && previous && previous.as_of < data.as_of ? previous : undefined
  const items = AXES.map(key => (fallback?.axes ?? data?.observations)?.find(o => o.key === key))
  const valid = (fallback || (data?.version === 'observations-v1' && data.coverage.method_current && !pendingClose))
    && items.every(o => o?.percentile != null)
  if (!valid) {
    const current = data?.observations.filter(o => AXES.includes(o.key)) ?? []
    const ready = current.filter(o => o.percentile != null).length
    const counts = current.map(o => o.history_sample_count)
    const min = counts.length ? Math.min(...counts) : 0
    const max = counts.length ? Math.max(...counts) : 0
    const reason = !data || data.version !== 'observations-v1' ? '尚无可用的统一口径数据。'
      : !data.coverage.method_current ? '历史口径尚未更新，需重算市场环境。'
      : pendingClose ? '当日收盘口径尚未确认，正式日线同步并重算市场环境后更新。'
      : `六项中${ready}项已有完整500日分位；各轴有效历史${min === max ? min : `${min}–${max}`}日。`
    return <div className="space-y-1 rounded bg-elevated/40 p-2 text-[11px] leading-4 text-muted">
      <p>雷达暂不可计算：{reason}</p>
      <p>下方显示各项历史进度与缺数原因。{!pendingClose && '历史不足时，可在数据页向前扩展股票及指数日线，再重算市场环境。'}</p>
    </div>
  }
  const point = (i: number, r: number) => [150 + Math.cos(-Math.PI / 2 + i * Math.PI / 3) * r, 115 + Math.sin(-Math.PI / 2 + i * Math.PI / 3) * r]
  const polygon = (scale: number) => items.map((_, i) => point(i, scale * 70).join(',')).join(' ')
  return <>
    {fallback && <div role="status" className="space-y-1 text-[11px] leading-4">
      <p className="text-warning">{data?.as_of} 收盘口径尚未确认</p>
      <p className="text-secondary">雷达日期 {fallback.as_of} · 最近可用收盘日</p>
    </div>}
    <svg viewBox="0 0 300 230" className="mx-auto h-44 w-full" role="img" aria-label={`${fallback?.as_of ?? data?.as_of} 情绪雷达 · 六项独立历史分位`}>
    {[1, .5].map(scale => <polygon key={scale} points={polygon(scale)} fill="none" stroke="hsl(var(--border))" />)}
    {items.map((o, i) => { const [x, y] = point(i, 70); const [tx, ty] = point(i, 100); return <g key={o!.key}><line x1="150" y1="115" x2={x} y2={y} stroke="hsl(var(--border))" /><text x={tx} y={ty} textAnchor="middle" className="fill-muted text-[9px]">{o!.label}<tspan x={tx} dy="12">P{o!.percentile!.toFixed(0)}</tspan></text></g> })}
    <polygon points={items.map((o, i) => point(i, o!.percentile! * .7).join(',')).join(' ')} fill="hsl(var(--accent) / .15)" stroke="hsl(var(--accent))" strokeWidth="1.5" />
    </svg>
  </>
}
