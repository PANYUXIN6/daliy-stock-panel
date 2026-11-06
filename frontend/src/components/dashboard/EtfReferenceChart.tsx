import { useEffect, useMemo, useRef } from 'react'
import * as echarts from 'echarts'
import { LineChart, Loader2 } from 'lucide-react'
import type { EtfReference } from '@/lib/api'
import { useChartTheme } from '@/lib/theme'

export default function EtfReferenceChart({ data, loading, error, onRetry }: {
  data?: EtfReference
  loading: boolean
  error: boolean
  onRetry: () => void
}) {
  const container = useRef<HTMLDivElement>(null)
  const theme = useChartTheme()
  const rows = data?.rows
  const hasRows = !!rows?.length
  const option = useMemo<echarts.EChartsOption>(() => ({
    animation: false,
    grid: { left: 12, right: 18, top: 15, bottom: 42, containLabel: true },
    tooltip: {
      trigger: 'axis', renderMode: 'richText', confine: true,
      backgroundColor: theme.tooltipBg, borderColor: theme.tooltipBorder,
      textStyle: { color: theme.tooltipText, fontSize: 11 },
      valueFormatter: value => `${(Number(value) * 100).toFixed(3)}%`,
    },
    xAxis: {
      type: 'category', data: rows?.map(row => row.date) ?? [], boundaryGap: false,
      axisLabel: { color: theme.text, fontSize: 10, hideOverlap: true },
      axisLine: { lineStyle: { color: theme.border } }, axisTick: { show: false },
    },
    yAxis: {
      type: 'value', scale: true,
      axisLabel: { color: theme.text, fontSize: 10, formatter: (value: number) => `${(value * 100).toFixed(2)}%` },
      splitLine: { lineStyle: { color: theme.grid, type: 'dashed' } },
    },
    dataZoom: [
      { type: 'inside', filterMode: 'none' },
      { type: 'slider', height: 14, bottom: 4, borderColor: 'transparent',
        textStyle: { color: theme.text, fontSize: 9 }, showDetail: false,
        fillerColor: theme.zoomFill, dataBackground: { lineStyle: { color: theme.border } } },
    ],
    series: [{ name: '国家队持仓占比参考', type: 'line', data: rows?.map(row => row.ratio) ?? [],
      showSymbol: false, connectNulls: false, lineStyle: { width: 2, color: '#d7a658' },
      itemStyle: { color: '#d7a658' } }],
  }), [rows, theme])

  useEffect(() => {
    if (!container.current || !hasRows) return
    const chart = echarts.init(container.current)
    chart.setOption(option)
    const observer = new ResizeObserver(() => chart.resize())
    observer.observe(container.current)
    return () => { observer.disconnect(); chart.dispose() }
  }, [option, hasRows])

  const latest = rows?.at(-1)
  return (
    <section className="mb-1.5 min-w-0 overflow-hidden rounded-card border border-border bg-surface/80 p-2 shadow-[0_1px_2px_hsl(var(--border)/0.4)]" aria-label="国家队持仓占比参考">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-1">
        <h2 className="flex items-center gap-1.5 text-xs font-semibold text-foreground">
          <LineChart className="h-3.5 w-3.5 text-accent" />国家队持仓占比参考
        </h2>
        <div className="flex items-baseline gap-3">
          {latest && <span className="font-mono text-sm font-semibold text-foreground">{(latest.ratio * 100).toFixed(3)}%</span>}
          {data?.as_of && <span className="text-[10px] text-muted">截至 {data.as_of}</span>}
        </div>
      </div>
      {/* 图表销毁会清空容器；不同 key 避免 React 将其复用为加载/空态节点。 */}
      {hasRows ? <div key="chart" ref={container} className="h-[210px] w-full sm:h-[240px]" role="img" aria-label={`国家队持仓占比参考，最新${(latest!.ratio * 100).toFixed(3)}%，截至${data?.as_of}`} /> : (
        <div key="status" className="flex h-[210px] flex-wrap content-center items-center justify-center gap-2 px-2 text-center text-xs text-muted sm:h-[240px]" role="status">
          {loading ? <><Loader2 className="h-3.5 w-3.5 animate-spin" />加载中…</> : error ? <>
            加载失败<button className="text-accent hover:underline" onClick={onRetry}>重试</button>
          </> : data?.update_failed ? <>
            <span className="w-full text-warning">数据采集失败，尚无可展示数据</span>
            <span>{data.failure_stage ? `未完成：${data.failure_stage}` : '请在数据页查看采集任务的失败记录'}</span>
          </> : '暂无已对齐的数据'}
        </div>
      )}
      {(error || data?.update_failed) && hasRows && <p className="px-1 text-[10px] text-warning">更新失败，显示已保存数据{data?.failure_stage && `；未完成：${data.failure_stage}`}</p>}
    </section>
  )
}
