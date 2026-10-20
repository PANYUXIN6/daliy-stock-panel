import { useEffect, useMemo, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { type MinuteKlineRow } from '@/lib/api'
import { klineMinuteQueryOptions, minuteRefetchInterval } from '@/lib/kline'
import { markMinuteBatchForFullRead } from '@/lib/minuteBatchIncremental'
import { minuteCollectionAffectsQuery, QK } from '@/lib/queryKeys'
import { EChartsIntraday } from '@/components/EChartsIntraday'
import { MinuteCollectionButton } from '@/components/MinuteCollectionButton'
import type { DailySummary } from '@/lib/intraday-chart'

interface Props {
  symbol: string
  date: string | null
  height?: number
  prevClose?: number
  dailySummary?: DailySummary
  className?: string
  onPriceHover?: (price: number | null) => void
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
  currentPrice?: number
  priceLines?: { value: number; label?: string; color?: string }[]
  /** 自动刷新间隔(ms)。undefined/0 = 不轮询(默认)。个股对话框盘中实时刷新时传入。 */
  refetchIntervalMs?: number
}

export function StockIntradayChart({
  symbol,
  date,
  height = 520,
  prevClose,
  dailySummary,
  className,
  onPriceHover,
  onPriceDoubleClick,
  currentPrice,
  priceLines,
  refetchIntervalMs,
}: Props) {
  const qc = useQueryClient()
  const minute = useQuery({
    // 读取后台保存的最新数据，轮询不会触发供应商请求。
    ...klineMinuteQueryOptions(symbol, date ?? undefined, refetchIntervalMs != null),
    enabled: !!symbol && !!date,
    refetchInterval: minuteRefetchInterval(refetchIntervalMs),
  })

  const minuteRows: MinuteKlineRow[] = useMemo(() => minute.data?.rows ?? [], [minute.data?.rows])
  const hasMinuteRows = minuteRows.length > 0
  const previous = useRef<{ symbol: string; date: string | null; empty: boolean } | null>(null)
  useEffect(() => {
    const sameContext = previous.current?.symbol === symbol && previous.current.date === date
    if (sameContext && previous.current?.empty && minute.isSuccess && hasMinuteRows && date) {
      // SSE 可能先于任务终态送达并卸载空态按钮，按实际数据到达刷新相关列表分时。
      markMinuteBatchForFullRead(qc, symbol, date)
      void qc.invalidateQueries({
        predicate: query => query.queryKey[0] === QK.minuteBatch('')[0]
          && minuteCollectionAffectsQuery(query.queryKey, symbol, date),
      })
    }
    previous.current = {
      symbol, date,
      empty: minute.isSuccess ? !hasMinuteRows : sameContext && previous.current?.empty === true,
    }
  }, [symbol, date, minute.isSuccess, hasMinuteRows, qc])
  useEffect(() => {
    onPriceHover?.(null)
  }, [date, onPriceHover])

  if (!symbol || !date) return null

  return (
    <div className={className} style={{ height, flexShrink: 0 }}>
      {minute.isLoading && <div className="text-xs text-muted py-2">分时加载中…</div>}
      {!minute.isLoading && minuteRows.length === 0 && (
        <div className="flex flex-col items-center justify-center h-full gap-3 text-xs">
          {minute.error ? (
            <span role="alert" className="text-danger">{minute.error.message}</span>
          ) : (
            <span className="text-muted">后台尚未采集 {date} 的分钟数据</span>
          )}
          {!minute.error && <MinuteCollectionButton key={`${symbol}:${date}`} symbol={symbol} date={date} />}
          <button
            type="button"
            onClick={() => void minute.refetch()}
            disabled={minute.isFetching}
            className="inline-flex items-center gap-1.5 rounded-btn border border-border bg-elevated px-3 py-1.5 text-secondary hover:text-foreground disabled:opacity-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${minute.isFetching ? 'animate-spin' : ''}`} />
            {minute.error ? '重新加载' : '刷新'}
          </button>
        </div>
      )}
      {minuteRows.length > 0 && (
        <EChartsIntraday
          data={minuteRows}
          height={height}
          prevClose={minute.data?.prev_close ?? prevClose}
          dailySummary={dailySummary}
          date={date}
          priceLimit={minute.data?.price_limit ?? undefined}
          onPriceHover={onPriceHover}
          onPriceDoubleClick={onPriceDoubleClick}
          currentPrice={currentPrice}
          priceLines={priceLines}
        />
      )}
    </div>
  )
}
