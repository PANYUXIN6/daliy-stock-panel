import { MethodHelp } from '@/components/MethodHelp'
import { useEffect, useMemo, useRef } from 'react'
import * as echarts from 'echarts/core'
import { LineChart, BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent, DataZoomComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { EChartsOption, LineSeriesOption, BarSeriesOption } from 'echarts'
import { useChartTheme } from '@/lib/theme'

echarts.use([LineChart, BarChart, GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, CanvasRenderer])

export interface FlowSeries {
  name: string
  data: (number | null)[]
  color: string
  type?: 'line' | 'bar'
  index?: boolean
  signed?: boolean
  stack?: string
}

export function FlowChart({ title, labels, series, zoom = false, empty = '暂无数据', help }: {
  title: string; labels: string[]; series: FlowSeries[]; zoom?: boolean; empty?: string; help?: string
}) {
  const container = useRef<HTMLDivElement>(null)
  const instance = useRef<echarts.ECharts | null>(null)
  const theme = useChartTheme()
  const hasData = series.some(s => s.data.some(value => value != null))
  const option = useMemo<EChartsOption>(() => ({
    animation: false,
    color: series.map(s => s.color),
    grid: { left: 12, right: 15, top: 64, bottom: zoom ? 42 : 16, containLabel: true },
    legend: { type: 'scroll', top: 6, left: 10, right: 10, textStyle: { color: theme.text, fontSize: 10 },
      pageTextStyle: { color: theme.text }, pageIconColor: theme.text },
    tooltip: { trigger: 'axis', confine: true, renderMode: 'richText',
      backgroundColor: theme.tooltipBg, borderColor: theme.tooltipBorder, textStyle: { color: theme.tooltipText },
      valueFormatter: value => value == null || value === '-' ? '—' : Number(value).toFixed(2) },
    xAxis: { type: 'category', data: labels, boundaryGap: series.some(s => s.type === 'bar'),
      axisLabel: { color: theme.text, fontSize: 10, hideOverlap: true },
      axisLine: { lineStyle: { color: theme.border } }, axisTick: { show: false } },
    yAxis: [
      { type: 'value', name: '亿元', nameTextStyle: { color: theme.text, fontSize: 10 },
        axisLabel: { color: theme.text, fontSize: 10 }, splitLine: { lineStyle: { color: theme.grid, type: 'dashed' } } },
      { type: 'value', name: series.some(s => s.index) ? '指数点位' : '', show: series.some(s => s.index), scale: true,
        nameTextStyle: { color: theme.text, fontSize: 10 }, axisLabel: { color: theme.text, fontSize: 10 }, splitLine: { show: false } },
    ],
    dataZoom: zoom ? [{ type: 'inside', filterMode: 'none' },
      { type: 'slider', height: 15, bottom: 0, showDetail: false, borderColor: 'transparent',
        textStyle: { color: theme.text }, fillerColor: theme.zoomFill }] : [],
    series: series.map(s => {
      const base = { name: s.name, yAxisIndex: s.index ? 1 : 0, stack: s.stack,
        itemStyle: { color: s.color }, emphasis: { focus: 'series' as const } }
      if (s.type === 'bar') return { ...base, type: 'bar', barMaxWidth: 18,
        data: s.data.map(v => s.signed && v != null ? { value: v, itemStyle: { color: v >= 0 ? '#ef6464' : '#35b88a' } } : v),
      } satisfies BarSeriesOption
      return { ...base, type: 'line', data: s.data, showSymbol: false, connectNulls: false,
        lineStyle: { width: 1.8, color: s.color } } satisfies LineSeriesOption
    }),
  }), [labels, series, theme, zoom])

  useEffect(() => {
    if (!container.current || !hasData) return
    const chart = echarts.init(container.current)
    instance.current = chart
    const observer = new ResizeObserver(() => chart.resize())
    observer.observe(container.current)
    return () => { observer.disconnect(); chart.dispose(); instance.current = null }
  }, [hasData])
  useEffect(() => { instance.current?.setOption(option, true) }, [option, hasData])

  return <section className="min-w-0 rounded-card border border-border bg-surface p-4" aria-label={title}>
    <div className="flex items-center gap-1"><h3 className="text-sm font-semibold text-foreground">{title}</h3>{help && <MethodHelp label={`${title}口径`}><p>{help}</p></MethodHelp>}</div>
    {hasData ? <div key="chart" ref={container} className="mt-3 h-[290px] w-full sm:h-[320px]" role="img" aria-label={`${title}，${labels[0] ?? ''}至${labels.at(-1) ?? ''}`} />
      : <div key="empty" role="status" className="flex h-[290px] items-center justify-center text-xs text-muted sm:h-[320px]">{empty}</div>}
  </section>
}
