import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type BoxOscillation, type KlinePeriod } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

export const KLINE_PERIOD_LABELS: Record<KlinePeriod, string> = { daily: '日K', weekly: '周K', monthly: '月K' }
export const BOX_LEVEL_LABELS = { short: '小箱体', medium: '中箱体', long: '大箱体' } as const
export type BoxLevelFilter = 'all' | BoxOscillation['level']
export type BoxStatusFilter = 'all' | BoxOscillation['status']
export type KlineDateRange = { start: string; end: string }

function shiftDate(date: string, months: number): string {
  const value = new Date(`${date}T12:00:00Z`)
  const day = value.getUTCDate()
  value.setUTCDate(1)
  value.setUTCMonth(value.getUTCMonth() + months)
  const lastDay = new Date(Date.UTC(value.getUTCFullYear(), value.getUTCMonth() + 1, 0)).getUTCDate()
  value.setUTCDate(Math.min(day, lastDay))
  return value.toISOString().slice(0, 10)
}

export function getKlineDefaultRange(): KlineDateRange {
  const end = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date())
  return { start: shiftDate(end, -6), end }
}

/** 周月线保留足够的展示根数；历史回看始终保留调用方的观察截止日。 */
export function klineRangeForPeriod(range: KlineDateRange, period: KlinePeriod): KlineDateRange {
  if (period === 'daily') return range
  if (!range.end || Number.isNaN(new Date(`${range.end}T12:00:00Z`).getTime())) return range
  const start = shiftDate(range.end, period === 'weekly' ? -60 : -180)
  return { start: range.start < start ? range.start : start, end: range.end }
}

export function boxStatusLabel(box: BoxOscillation): string {
  const direction = box.breakout.direction === 'up' ? '向上' : '向下'
  if (box.status === 'broken') return `已${direction}突破`
  if (box.status === 'invalidated') return '结构失效'
  if (box.status === 'candidate') return '形成中候选'
  if (box.breakout.status === 'pending') return `${direction}突破待确认`
  if (box.breakout.status === 'returned') return '已形成 · 越界后回箱'
  return '已形成'
}

export function useBoxOscillation(symbol: string, range: KlineDateRange, startOverrides?: Partial<Record<KlinePeriod, string>>) {
  const [period, setPeriod] = useState<KlinePeriod>('daily')
  const [enabled, setEnabled] = useState(false)
  const [level, setLevel] = useState<BoxLevelFilter>('all')
  const [status, setStatus] = useState<BoxStatusFilter>('all')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [showAll, setShowAll] = useState(false)
  const expandedRange = klineRangeForPeriod(range, period)
  const requestRange = { ...expandedRange, start: startOverrides?.[period] ?? expandedRange.start }
  const rangeError = !requestRange.start || !requestRange.end ? '请选择完整的起止日期'
    : requestRange.start > requestRange.end ? '起始日期不能晚于截止日期' : null
  const query = useQuery({
    queryKey: QK.klineBoxes(symbol, period, requestRange.start, requestRange.end),
    queryFn: () => api.klineBoxes(symbol, period, requestRange),
    enabled: !!symbol && !rangeError && (enabled || period !== 'daily'),
    staleTime: 30_000,
    refetchInterval: requestRange.end >= getKlineDefaultRange().end ? 30_000 : false,
  })
  const report = !rangeError && !query.isError && query.data?.symbol === symbol && query.data.period === period ? query.data : undefined
  const filteredBoxes = useMemo(() => (report?.boxes ?? [])
    .filter(box => box.end_date >= requestRange.start && box.start_date <= requestRange.end
      && (level === 'all' || box.level === level) && (status === 'all' || box.status === status))
    .sort((a, b) => Number(b.status === 'confirmed') - Number(a.status === 'confirmed')
      || b.observed_until.localeCompare(a.observed_until) || b.start_date.localeCompare(a.start_date)),
  [report, level, status, requestRange.start, requestRange.end])
  const selectedBox = filteredBoxes.find(box => box.id === selectedId) ?? filteredBoxes[0]
  const boxes = useMemo(() => enabled ? (showAll ? filteredBoxes : selectedBox ? [selectedBox] : []) : [],
    [enabled, showAll, filteredBoxes, selectedBox])
  return { period, setPeriod, enabled, setEnabled, level, setLevel, status, setStatus, showAll, setShowAll,
    selectedBox, setSelectedId, boxes, filteredBoxes, report, query, requestRange, rangeError }
}

export type BoxOscillationView = ReturnType<typeof useBoxOscillation>
