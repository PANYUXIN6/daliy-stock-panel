import type { SentimentObservations } from '@/lib/api'

// 每条轴独立表示历史分位；缺项不填零，也不合成情绪分。
const AXES = ['up_pct', 'above_ma20_pct', 'total_amount', 'index_position_60', 'limit_up_pct', 'seal_rate']
export function SentimentRadar({ data }: { data?: SentimentObservations | null }) {
  const items = AXES.map(key => data?.observations.find(o => o.key === key))
  const valid = data?.version === 'observations-v1' && items.every(o => o?.percentile != null)
  if (!valid) return <p className="rounded bg-elevated/40 p-2 text-[11px] text-muted">雷达暂不可计算：各轴需有完整500交易日历史分位。下方仍展示可用原值。</p>
  const point = (i: number, r: number) => [150 + Math.cos(-Math.PI / 2 + i * Math.PI / 3) * r, 115 + Math.sin(-Math.PI / 2 + i * Math.PI / 3) * r]
  const polygon = (scale: number) => items.map((_, i) => point(i, scale * 70).join(',')).join(' ')
  return <svg viewBox="0 0 300 230" className="mx-auto h-44 w-full" role="img" aria-label="六项独立历史分位雷达，不合成总分">
    {[1, .5].map(scale => <polygon key={scale} points={polygon(scale)} fill="none" stroke="hsl(var(--border))" />)}
    {items.map((o, i) => { const [x, y] = point(i, 70); const [tx, ty] = point(i, 100); return <g key={o!.key}><line x1="150" y1="115" x2={x} y2={y} stroke="hsl(var(--border))" /><text x={tx} y={ty} textAnchor="middle" className="fill-muted text-[9px]">{o!.label}<tspan x={tx} dy="12">P{o!.percentile!.toFixed(0)}</tspan></text></g> })}
    <polygon points={items.map((o, i) => point(i, o!.percentile! * .7).join(',')).join(' ')} fill="hsl(var(--accent) / .15)" stroke="hsl(var(--accent))" strokeWidth="1.5" />
  </svg>
}
