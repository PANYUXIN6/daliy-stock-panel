/** 迷你分时折线图（自选列表共享）。

用当日分钟K的 close 画一条折线 + 昨收水平基准线 + 分时均线。
风格仿同花顺/东方财富分时图：
- 价格折线：涨（收盘 ≥ 昨收）红色，跌绿色 — 以昨收价(prevClose)为基准, 不是当日开盘价
- 昨收基准线：浅灰实线（从开到右），比虚线更明显
- 分时均线：黄色细线（成交均价，这里用 close 的累计均值近似）
- 价格线下方渐变填充: 顶部半透明实色 → 底部全透明(与个股对话框 EChartsIntraday 一致)
有有效成交量时在底部显示独立量带；小图按宽度合并相邻分钟，保留成交量总和。
空数据返回等尺寸占位 SVG，保证加载前后尺寸一致（同 MiniCandlestick 模式）。
*/
import { useId } from 'react'
import type { MinuteKlineRow } from '@/lib/api'

export function MiniIntraday({ rows, prevClose, changePct, width = 100, height = 56 }: {
  rows: MinuteKlineRow[]
  /** 昨收价 (前收), 用于基准线。无则用 close/changePct 反算 */
  prevClose?: number | null
  /** 涨跌幅 (小数, 如 -0.029 = -2.9%), 用于涨跌着色。优先级最高 */
  changePct?: number | null
  width?: number
  height?: number
}) {
  // 空态切换到有数据时也保持 Hook 顺序。
  const gradId = useId().replace(/:/g, '')
  // 空数据：返回等尺寸占位
  if (!rows || rows.length < 2) {
    return <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className="block" aria-label="暂无分时" />
  }

  const BULL = '#C74040'
  const BEAR = '#2D9B65'
  const LINE_PREV_CLOSE = '#7A7A85'   // 昨收基准线: 深灰实线
  const LINE_AVG = '#E0B84A'          // 均线: 暖黄

  const W = width
  const H = height
  const padY = 3
  const n = rows.length

  // 涨跌着色: 优先用 changePct (后端 enriched 字段, 最可靠);
  // 其次用 prevClose vs lastClose; 最后回退到首根 open(可能为 null, 再退首根 close)
  const lastClose = rows[n - 1].close
  const firstRef = rows[0].open ?? rows[0].close
  const isUp = changePct != null
    ? changePct >= 0
    : prevClose != null && prevClose > 0
      ? lastClose >= prevClose
      : lastClose >= firstRef
  const color = isUp ? BULL : BEAR

  // 昨收基准线: 优先用 prevClose; 其次用 changePct 反算 (close/(1+changePct));
  // 最后回退到首根 open(为 null 时退首根 close)
  const baseline = (prevClose != null && prevClose > 0)
    ? prevClose
    : (changePct != null && changePct !== 0)
      ? lastClose / (1 + changePct)
      : firstRef

  // 价格区间: close + 昨收 + 均线 全部纳入, 确保都在可视范围
  let hi = -Infinity, lo = Infinity
  // 累计均价 (分时均线的近似: close 的累计平均)
  const avgLine: number[] = []
  let cumSum = 0
  for (let i = 0; i < n; i++) {
    const c = rows[i].close
    cumSum += c
    const avg = cumSum / (i + 1)
    avgLine.push(avg)
    if (c > hi) hi = c
    if (c < lo) lo = c
    if (avg > hi) hi = avg
    if (avg < lo) lo = avg
  }
  // 把昨收也纳入区间
  hi = Math.max(hi, baseline)
  lo = Math.min(lo, baseline)
  const range = hi - lo || 1

  const hasVolume = H >= 30 && rows.some(row => Number.isFinite(row.volume) && row.volume > 0)
  const priceBottom = hasVolume ? Math.round(H * 0.66) : H - padY
  const volumeTop = priceBottom + 3
  const volumeBottom = H - padY
  const yScale = (v: number) => padY + (1 - (v - lo) / range) * (priceBottom - padY)
  const xScale = (i: number) => (i / (n - 1)) * W

  // 窄图中一根柱合并若干相邻分钟，按同一横轴分桶；不把缺量、负量当成交。
  const volumeBuckets = new Map<number, { volume: number; first: number; last: number }>()
  const bucketCount = Math.min(n, Math.max(1, Math.floor(W / 2.5)))
  const bucketWidth = W / bucketCount
  if (hasVolume) {
    rows.forEach((row, index) => {
      if (!Number.isFinite(row.volume) || row.volume < 0) return
      const bucket = Math.min(bucketCount - 1, Math.floor(xScale(index) / bucketWidth))
      const existing = volumeBuckets.get(bucket)
      if (existing) {
        existing.volume += row.volume
        existing.last = index
      } else {
        volumeBuckets.set(bucket, { volume: row.volume, first: index, last: index })
      }
    })
  }
  const maxVolume = Math.max(0, ...Array.from(volumeBuckets.values(), bucket => bucket.volume))
  const volumeBars = Array.from(volumeBuckets, ([bucket, value]) => {
    const ref = value.first > 0 ? rows[value.first - 1].close : rows[value.first].open
    const close = rows[value.last].close
    const fill = ref == null || close === ref ? LINE_PREV_CLOSE : close > ref ? BULL : BEAR
    return {
      bucket,
      volume: value.volume,
      x: (bucket + 0.15) * bucketWidth,
      height: maxVolume > 0 ? value.volume / maxVolume * (volumeBottom - volumeTop) : 0,
      fill,
    }
  }).filter(bar => bar.volume > 0)

  // 价格折线 points
  const pricePoints = rows.map((r, i) => `${xScale(i).toFixed(1)},${yScale(r.close).toFixed(1)}`).join(' ')
  // 均线 points
  const avgPoints = avgLine.map((v, i) => `${xScale(i).toFixed(1)},${yScale(v).toFixed(1)}`).join(' ')

  // 渐变填充多边形 points: 价格折线 + 底部右下角 + 左下角, 闭合到画布底边
  const bottomY = priceBottom
  const areaPoints = `${pricePoints} ${xScale(n - 1).toFixed(1)},${bottomY.toFixed(1)} ${xScale(0).toFixed(1)},${bottomY.toFixed(1)}`

  // 昨收参考线 y 坐标
  const prevCloseY = yScale(baseline)

  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="block" aria-label={hasVolume ? '分时走势与成交量' : '分时走势'}>
      <defs>
        <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={color} stopOpacity={0.4} />
          <stop offset="1" stopColor={color} stopOpacity={0} />
        </linearGradient>
      </defs>
      {hasVolume && (
        <g aria-label="成交量">
          <line x1={0} y1={volumeTop - 1.5} x2={W} y2={volumeTop - 1.5} stroke={LINE_PREV_CLOSE} strokeWidth={0.5} opacity={0.3} />
          {volumeBars.map(bar => (
            <rect key={bar.bucket} x={bar.x} y={volumeBottom - bar.height} width={bucketWidth * 0.7} height={bar.height} fill={bar.fill} opacity={0.65}>
              <title>{`成交量 ${bar.volume.toLocaleString('zh-CN')}`}</title>
            </rect>
          ))}
        </g>
      )}
      {/* 昨收基准线 (深灰实线, 比虚线更明显) */}
      <line
        x1={0} y1={prevCloseY} x2={W} y2={prevCloseY}
        stroke={LINE_PREV_CLOSE} strokeWidth={0.6} opacity={0.7}
      />
      {/* 价格折线下方渐变填充 */}
      <polygon points={areaPoints} fill={`url(#${gradId})`} stroke="none" />
      {/* 分时均线 (暖黄细线) */}
      <polyline
        points={avgPoints}
        fill="none"
        stroke={LINE_AVG}
        strokeWidth={0.8}
        strokeLinejoin="round"
        strokeLinecap="round"
        opacity={0.85}
      />
      {/* 分时价格折线 */}
      <polyline
        points={pricePoints}
        fill="none"
        stroke={color}
        strokeWidth={1.1}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  )
}
