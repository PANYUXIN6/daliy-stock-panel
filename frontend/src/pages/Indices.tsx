import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Activity, RefreshCw } from 'lucide-react'
import { api, type IndexInstrument, type KlinePeriod, type KlineRow, type MinuteKlineRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { EChartsCandlestick, type OHLC } from '@/components/EChartsCandlestick'
import { EChartsIntraday } from '@/components/EChartsIntraday'
import { BoxOscillationControls } from '@/components/BoxOscillationControls'
import { getKlineDefaultRange, KLINE_PERIOD_LABELS, useBoxOscillation } from '@/lib/useBoxOscillation'

function defaultRange() {
  return getKlineDefaultRange()
}

function toOHLC(rows: KlineRow[]): OHLC[] {
  return rows
    .filter(r => r?.date != null && r.open != null && r.close != null)
    .map(r => ({
      date: typeof r.date === 'string' ? r.date.slice(0, 10) : String(r.date),
      open: Number(r.open),
      high: Number(r.high),
      low: Number(r.low),
      close: Number(r.close),
      volume: Number(r.volume ?? 0),
      ma5: r.ma5 != null ? Number(r.ma5) : null,
      ma10: r.ma10 != null ? Number(r.ma10) : null,
      ma20: r.ma20 != null ? Number(r.ma20) : null,
      ma60: r.ma60 != null ? Number(r.ma60) : null,
      bull_bear_line: r.bull_bear_line != null ? Number(r.bull_bear_line) : null,
      macd_dif: r.macd_dif != null ? Number(r.macd_dif) : null,
      macd_dea: r.macd_dea != null ? Number(r.macd_dea) : null,
      macd_hist: r.macd_hist != null ? Number(r.macd_hist) : null,
      rsi_6: r.rsi_6 != null ? Number(r.rsi_6) : null,
      rsi_14: r.rsi_14 != null ? Number(r.rsi_14) : null,
      rsi_24: r.rsi_24 != null ? Number(r.rsi_24) : null,
      kdj_k: r.kdj_k != null ? Number(r.kdj_k) : null,
      kdj_d: r.kdj_d != null ? Number(r.kdj_d) : null,
      kdj_j: r.kdj_j != null ? Number(r.kdj_j) : null,
      boll_upper: r.boll_upper != null ? Number(r.boll_upper) : null,
      boll_lower: r.boll_lower != null ? Number(r.boll_lower) : null,
    }))
}

function fmtPct(v: number | null | undefined) {
  if (v == null || Number.isNaN(Number(v))) return '--'
  return `${Number(v).toFixed(2)}%`
}

function fmtNum(v: number | null | undefined, digits = 2) {
  if (v == null || Number.isNaN(Number(v))) return '--'
  return Number(v).toFixed(digits)
}

const PINNED_INDEXES = [
  { symbol: '000001.SH', name: '上证指数' },
  { symbol: '399001.SZ', name: '深证成指' },
  { symbol: '399006.SZ', name: '创业板指' },
  { symbol: '000680.SH', name: '科创综指' },
]

export function Indices() {
  const [searchParams, setSearchParams] = useSearchParams()
  const symbolParam = searchParams.get('symbol') ?? ''
  const [selected, setSelected] = useState<string>(symbolParam)
  const [range, setRange] = useState(defaultRange)
  const [startOverrides, setStartOverrides] = useState<Partial<Record<KlinePeriod, string>>>({})
  const [selectedDate, setSelectedDate] = useState<string | null>(null)
  const [linkedPrice, setLinkedPrice] = useState<number | null>(null)

  // 指数标的固定核心四只 (产品契约, 不再提供全指数搜索/浏览)
  const topRows: IndexInstrument[] = PINNED_INDEXES.map(p => ({
    symbol: p.symbol, name: p.name, asset_type: 'index' as const,
  }))

  const selectedSymbol = selected || topRows[0]?.symbol || ''
  const boxView = useBoxOscillation(selectedSymbol, range, startOverrides)
  const isDaily = boxView.period === 'daily'

  useEffect(() => {
    if (symbolParam && symbolParam !== selected) setSelected(symbolParam)
  }, [selected, symbolParam])

  const selectIndex = (symbol: string) => {
    setSelected(symbol)
    setSearchParams({ symbol })
  }

  const quotes = useQuery({
    queryKey: QK.indexQuotes,
    queryFn: () => api.indexQuotes(),
    placeholderData: (prev) => prev,
  })

  const daily = useQuery({
    queryKey: QK.indexDaily(selectedSymbol, boxView.requestRange.start, range.end),
    queryFn: () => api.indexDaily(selectedSymbol, 180, boxView.requestRange),
    enabled: !!selectedSymbol && isDaily && !boxView.rangeError,
    placeholderData: (prev) => prev?.symbol === selectedSymbol ? prev : undefined,
  })

  const minute = useQuery({
    queryKey: QK.indexMinute(selectedSymbol, selectedDate ?? ''),
    queryFn: () => api.indexMinute(selectedSymbol, selectedDate ?? undefined),
    enabled: !!selectedSymbol && !!selectedDate,
  })

  // 周月K点击仍查看该根最后一个交易日的分时；其昨收取实际日K，不能用上根周月收盘替代。
  const minuteDailyRange = useMemo(() => {
    if (!selectedDate) return null
    const start = new Date(`${selectedDate}T12:00:00Z`)
    start.setUTCDate(start.getUTCDate() - 30)
    return { start: start.toISOString().slice(0, 10), end: selectedDate }
  }, [selectedDate])
  const minuteDaily = useQuery({
    queryKey: QK.indexDaily(selectedSymbol, minuteDailyRange?.start ?? '', minuteDailyRange?.end ?? ''),
    queryFn: () => api.indexDaily(selectedSymbol, 30, minuteDailyRange!),
    enabled: !!selectedSymbol && !isDaily && !!minuteDailyRange,
  })

  const quoteBySymbol = useMemo(() => {
    const m = new Map<string, any>()
    for (const q of quotes.data?.rows ?? []) m.set(q.symbol, q)
    return m
  }, [quotes.data?.rows])
  const selectedQuote = selectedSymbol ? quoteBySymbol.get(selectedSymbol) : null
  const selectedQuoteValue = selectedQuote?.last_price ?? selectedQuote?.price ?? selectedQuote?.close
  const selectedQuotePct = selectedQuote?.change_pct ?? selectedQuote?.pct

  const currentRows = isDaily ? daily.data?.rows : boxView.report?.rows
  const currentSymbol = isDaily ? daily.data?.symbol : boxView.report?.symbol
  const chartQuery = isDaily ? daily : boxView.query
  const chartRows = useMemo(() => toOHLC(currentRows ?? []), [currentRows])
  const selectedInfo = topRows.find(r => r.symbol === selectedSymbol) || daily.data?.index_info
  const minuteRows: MinuteKlineRow[] = minute.data?.symbol === selectedSymbol
    && minute.data?.date === selectedDate && currentSymbol === selectedSymbol
    ? minute.data.rows : []
  const dailyRows = isDaily ? chartRows : minuteDaily.data?.symbol === selectedSymbol ? minuteDaily.data.rows : []
  const selectedIdx = selectedDate ? dailyRows.findIndex(r => r.date === selectedDate) : -1
  const prevClose = selectedIdx > 0
    ? dailyRows[selectedIdx - 1].close
    : isDaily && dailyRows.length >= 2
      ? dailyRows[dailyRows.length - 2].close
      : undefined

  useEffect(() => {
    setSelectedDate(null)
    setLinkedPrice(null)
  }, [selectedSymbol, boxView.period])

  useEffect(() => {
    if ((!selectedDate || !chartRows.some(r => r.date === selectedDate)) && chartRows.length > 0 && currentSymbol === selectedSymbol) {
      setSelectedDate(chartRows[chartRows.length - 1].date)
    }
  }, [chartRows, currentSymbol, selectedDate, selectedSymbol])
  const renderIndexItem = (item: IndexInstrument) => {
    const q = quoteBySymbol.get(item.symbol)
    const pct = q?.change_pct ?? q?.pct
    const current = q?.last_price ?? q?.price ?? q?.close
    const active = item.symbol === selectedSymbol
    return (
      <button
        key={item.symbol}
        onClick={() => selectIndex(item.symbol)}
        className={`w-full rounded-btn px-2 py-2 text-left transition-colors ${active ? 'bg-accent/15 text-foreground' : 'hover:bg-elevated text-secondary'}`}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-xs font-medium">{item.name || item.symbol}</span>
          <span className={`text-[10px] font-mono ${Number(pct ?? 0) >= 0 ? 'text-bull' : 'text-bear'}`}>{fmtPct(pct)}</span>
        </div>
        <div className="mt-0.5 flex items-center justify-between text-[10px] font-mono text-muted">
          <span>{item.symbol}</span>
          <span>{fmtNum(current)}</span>
        </div>
      </button>
    )
  }

  return (
    <div className="h-full overflow-auto bg-base p-4">
      <div className="mb-4 flex items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-foreground">指数</h1>
          <p className="mt-1 text-xs text-muted">
            查看后台已采集的指数日、周、月K线、箱体与分时数据。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => { void quotes.refetch(); if (isDaily) void daily.refetch(); if (boxView.enabled || !isDaily) void boxView.query.refetch(); if (selectedDate) void minute.refetch() }}
            disabled={!!boxView.rangeError || quotes.isFetching || chartQuery.isFetching || boxView.query.isFetching || minute.isFetching}
            className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-btn border border-border bg-elevated px-3 py-1.5 text-xs text-secondary hover:text-foreground disabled:opacity-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${quotes.isFetching || chartQuery.isFetching || boxView.query.isFetching || minute.isFetching ? 'animate-spin' : ''}`} />刷新
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[15rem_1fr]">
        <aside className="rounded-card border border-border bg-surface p-3">
          <div className="mb-2 px-1 text-[11px] uppercase tracking-wider text-muted">核心指数</div>
          <div className="space-y-1">
            {topRows.map(renderIndexItem)}
          </div>
        </aside>

        <main className="min-w-0 rounded-card border border-border bg-surface p-3">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <Activity className="h-4 w-4 text-accent" />
                <h2 className="truncate text-sm font-semibold text-foreground">
                  {selectedInfo?.name || selectedSymbol || '未选择指数'}
                </h2>
                {selectedSymbol && <span className="font-mono text-xs text-muted">{selectedSymbol}</span>}
                {selectedSymbol && <span className="font-mono text-xs text-foreground">{fmtNum(selectedQuoteValue)}</span>}
                {selectedSymbol && <span className={`font-mono text-xs ${Number(selectedQuotePct ?? 0) >= 0 ? 'text-bull' : 'text-bear'}`}>{fmtPct(selectedQuotePct)}</span>}
              </div>
              <div className="mt-1 text-xs text-muted">
                实时缓存 {quotes.data?.count ?? 0} 只指数 · {KLINE_PERIOD_LABELS[boxView.period]}来源 {isDaily ? daily.data?.source ?? '--' : '本地日线聚合'}
              </div>
            </div>
            <div className="flex items-center gap-2 text-xs">
              <input
                type="date"
                aria-label="K线起始日期"
                value={boxView.requestRange.start}
                max={range.end}
                onChange={e => { if (e.target.value) setStartOverrides(current => ({ ...current, [boxView.period]: e.target.value })) }}
                className="rounded-btn border border-border bg-base px-2 py-1 text-secondary outline-none focus:border-accent"
              />
              <span className="text-muted">至</span>
              <input
                type="date"
                aria-label="K线截止日期"
                value={range.end}
                min={boxView.requestRange.start}
                onChange={e => { if (e.target.value) setRange(r => ({ ...r, end: e.target.value })) }}
                className="rounded-btn border border-border bg-base px-2 py-1 text-secondary outline-none focus:border-accent"
              />
            </div>
          </div>

          <BoxOscillationControls view={boxView} />
          {chartQuery.isLoading && <div className="py-10 text-center text-sm text-muted">{KLINE_PERIOD_LABELS[boxView.period]}加载中…</div>}
          {chartQuery.isError && <div className="py-4 text-sm text-danger">指数{KLINE_PERIOD_LABELS[boxView.period]}加载失败</div>}
          {!chartQuery.isLoading && !chartQuery.isError && chartRows.length === 0 && (
            <div className="rounded-card bg-elevated p-6 text-center text-sm text-muted">
              后台尚未采集该指数的{KLINE_PERIOD_LABELS[boxView.period]}数据。
            </div>
          )}
          {chartRows.length > 0 && (
            <div className="flex flex-col items-stretch gap-3 xl:flex-row xl:items-start">
              <div className="min-w-0 flex-1">
                <EChartsCandlestick
                  key={`${selectedSymbol}:${boxView.period}`}
                  data={chartRows}
                  height={620}
                  showMA={true}
                  showInfoBar={true}
                  showMarkers={false}
                  symbol={selectedSymbol}
                  linkedPrice={linkedPrice}
                  onDateClick={(date) => {
                    setSelectedDate(date)
                    setLinkedPrice(null)
                  }}
                  visibleBars={48}
                  activeIndicators={['vol', 'macd']}
                  boxes={boxView.boxes}
                />
              </div>
              <div className="min-w-0 flex-1 border-border xl:border-l xl:pl-3" style={{ height: 620 }}>
                {minute.isLoading && <div className="py-2 text-xs text-muted">分时加载中…</div>}
                {!minute.isLoading && minuteRows.length === 0 && (
                  <div className="flex h-full items-center justify-center text-xs text-muted">
                    暂无分时数据
                  </div>
                )}
                {minuteRows.length > 0 && (
                  <EChartsIntraday
                    key={`${selectedSymbol}:${selectedDate}`}
                    data={minuteRows}
                    height={620}
                    prevClose={prevClose}
                    date={selectedDate ?? undefined}
                    showLimitLines={false}
                    showAvgLine={false}
                    onPriceHover={setLinkedPrice}
                  />
                )}
              </div>
            </div>
          )}
        </main>
      </div>
    </div>
  )
}
