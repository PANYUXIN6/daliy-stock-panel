import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Loader2, RefreshCw } from 'lucide-react'
import { type MinuteKlineSession } from '@/lib/api'
import { klineMinuteQueryOptions, klineMinuteRangeQueryOptions, minuteRefetchInterval } from '@/lib/kline'
import { EChartsMultiDayIntraday } from '@/components/EChartsMultiDayIntraday'

interface Props {
  symbol: string
  days: number
  height?: number
  refetchIntervalMs?: number
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
  priceLines?: { value: number; label?: string; color?: string }[]
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '分钟数据读取失败'
}

export function StockMultiDayIntradayChart({
  symbol,
  days,
  height = 420,
  refetchIntervalMs,
  onPriceDoubleClick,
  priceLines,
}: Props) {
  const history = useQuery({
    ...klineMinuteRangeQueryOptions(symbol, days),
    enabled: !!symbol,
  })
  const latest = useQuery({
    // 读取后台保存的最新分钟数据，页面浏览不触发采集。
    ...klineMinuteQueryOptions(symbol, undefined, true),
    enabled: !!symbol,
    refetchInterval: minuteRefetchInterval(refetchIntervalMs),
  })

  const sessions = useMemo(() => {
    const byDate = new Map<string, MinuteKlineSession>()
    for (const session of history.data?.sessions ?? []) byDate.set(session.date, session)

    const latestDate = latest.data?.date
    const latestRows = latest.data?.rows ?? []
    if (latestDate && latestRows.length > 0) {
      const existing = byDate.get(latestDate)
      byDate.set(latestDate, {
        date: latestDate,
        prev_close: latest.data?.prev_close ?? existing?.prev_close ?? null,
        rows: latestRows,
      })
    }

    return Array.from(byDate.values())
      .sort((left, right) => left.date.localeCompare(right.date))
      .slice(-days)
  }, [days, history.data?.sessions, latest.data])

  const loading = sessions.length === 0 && (history.isLoading || latest.isLoading)
  const queryError = sessions.length === 0 ? history.error ?? latest.error : null
  const showCoverage = !history.isPlaceholderData && sessions.length > 0
  const chartHeight = Math.max(260, height - (showCoverage ? 32 : 0))
  const refreshing = history.isFetching || latest.isFetching
  const refresh = () => { void history.refetch(); void latest.refetch() }

  if (loading) {
    return (
      <div className="flex items-center justify-center gap-2 text-xs text-muted" style={{ height }}>
        <Loader2 className="h-4 w-4 animate-spin text-accent" />
        正在加载最近已落库 {days} 个交易日的分时…
      </div>
    )
  }

  if (queryError) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 text-xs" style={{ height }}>
        <span className="text-danger">{errorMessage(queryError)}</span>
        <button
          type="button"
          onClick={() => { void history.refetch(); void latest.refetch() }}
          className="inline-flex items-center gap-1.5 rounded-btn border border-border bg-elevated px-3 py-1.5 text-secondary hover:text-foreground"
        >
          <RefreshCw className="h-3.5 w-3.5" />
          重新加载
        </button>
      </div>
    )
  }

  if (sessions.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 text-xs" style={{ height }}>
        <span className="text-muted">后台尚未采集可展示的分钟数据</span>
        <button type="button" onClick={refresh} disabled={refreshing}
          className="inline-flex items-center gap-1.5 rounded-btn border border-border bg-elevated px-3 py-1.5 text-secondary hover:text-foreground disabled:opacity-50">
          <RefreshCw className={`h-3.5 w-3.5 ${refreshing ? 'animate-spin' : ''}`} />刷新
        </button>
      </div>
    )
  }

  return (
    <div style={{ height }}>
      {showCoverage && (
        <div className="flex h-8 items-center justify-between gap-3 border-b border-border/60 bg-elevated/40 px-3 text-[11px]">
          <span className="truncate text-muted" title={sessions.map(session => session.date).join('、')}>
            已采集 {sessions.length} / {days} 个交易日 · {sessions[0].date} ~ {sessions[sessions.length - 1].date}
          </span>
          <button type="button" onClick={refresh} disabled={refreshing}
            className="inline-flex shrink-0 items-center gap-1 text-secondary hover:text-foreground disabled:opacity-50">
            <RefreshCw className={`h-3 w-3 ${refreshing ? 'animate-spin' : ''}`} />刷新
          </button>
        </div>
      )}
      <EChartsMultiDayIntraday
        sessions={sessions}
        height={chartHeight}
        onPriceDoubleClick={onPriceDoubleClick}
        priceLines={priceLines}
      />

    </div>
  )
}
