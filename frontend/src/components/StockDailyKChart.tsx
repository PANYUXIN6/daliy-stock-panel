import { useCallback, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type KlineRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useTheme } from '@/lib/theme'
import { klineDailyQueryOptions } from '@/lib/kline'
import { storage } from '@/lib/storage'
import {
  EChartsCandlestick,
  OVERLAY_INDICATORS,
  SUB_CHARTS,
  type ChartMarker,
  type ChartPriceLine,
  type ChartRange,
  type OHLC,
  type VolumeCompareConfig,
} from '@/components/EChartsCandlestick'

const SUB_INFO_H = 16
const SUB_GAP = 4
const DEFAULT_VOLUME_COMPARE: VolumeCompareConfig = { enabled: true, days: 1 }

function normalizeVolumeCompare(config: VolumeCompareConfig): VolumeCompareConfig {
  return {
    enabled: config.enabled !== false,
    days: Math.max(1, Math.min(20, Math.round(Number(config.days) || 1))),
  }
}

interface Props {
  symbol: string
  height?: number
  className?: string
  dateRange?: { start: string; end: string }
  markers?: ChartMarker[]
  ranges?: ChartRange[]
  priceLines?: ChartPriceLine[]
  showLimitMarkers?: boolean
  showIndicatorControls?: boolean
  showMarkerToggle?: boolean
  showMA?: boolean
  showInfoBar?: boolean
  /** 初始可见蜡烛根数; 'all' = 适配显示全部数据 */
  visibleBars?: number | 'all'
  linkedPrice?: number | null
  onDateClick?: (date: string) => void
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
  /** 扩展数据列参数（逗号分隔 config_id.field_name），透传给 klineDaily 接口 */
  extColumns?: string
  /** 加入自选日 (北京时间 YYYY-MM-DD); 有值时日K主图绘制「自选」竖虚线 */
  addedDate?: string | null
}

function isValidRow(r: any): boolean {
  return r && r.date != null && r.open != null && r.close != null
}

export function toOHLC(rows: KlineRow[]): OHLC[] {
  return rows
    .filter(isValidRow)
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

function buildLimitUpMarkers(rows: KlineRow[]): ChartMarker[] {
  const markers: ChartMarker[] = []
  for (const r of rows) {
    const date = typeof r.date === 'string' ? r.date.slice(0, 10) : String(r.date)
    if (r.signal_broken_limit_up) {
      markers.push({ date, kind: 'neutral', above: true, color: '#8B5CF6', label: '炸' })
    } else if (r.signal_limit_up) {
      const boards: number = r.consecutive_limit_ups ?? 1
      markers.push({ date, kind: 'buy', above: true, color: '#FACC15', label: boards <= 1 ? '板' : String(boards) })
    }
  }
  return markers
}

const EARNINGS_COMMON_CONDITIONS = 'PSY(12)≤50，收盘价高于多空线；\n多空线或MA20上升，且当日为阳线。'
const GAP_RETEST_HINT = '缺口回踩\n断层后两根K线确认对应支撑，缺口支撑未跌破；\n确认后不足125个交易日，最低价回到参考低点附近\n（低于对应参考低点的1.03或1.05倍）。\n' + EARNINGS_COMMON_CONDITIONS
const MIDPOINT_RETEST_HINT = '中点回踩\n断层后连续4根K线的最低价高于断层当天K线的中点\n（中点＝当天最高价与最低价的平均值）；\n确认后第1至第9个交易日，中点支撑未跌破，\n且最低价低于断层当天的最高价。\n' + EARNINGS_COMMON_CONDITIONS

export function getDefaultRange(): { start: string; end: string } {
  const now = new Date()
  const end = now.toISOString().slice(0, 10)
  const s = new Date(now)
  s.setMonth(s.getMonth() - 6)
  const start = s.toISOString().slice(0, 10)
  return { start, end }
}

/** 工具栏小胶囊开关（指标与标注开关共用一套尺寸/关闭态样式，激活色由调用方给字面量）。 */
function ChartPill({
  active, label, activeClass, title, onClick,
}: {
  active: boolean
  label: string
  activeClass: string
  title?: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={`px-2 py-0.5 rounded text-[10px] font-mono cursor-pointer transition-colors ${
        active ? activeClass : 'bg-elevated text-muted hover:text-secondary'
      }`}
    >
      {label}
    </button>
  )
}

export function StockDailyKChart({
  symbol,
  height = 520,
  className,
  dateRange: externalDateRange,
  markers,
  ranges,
  priceLines,
  showLimitMarkers = true,
  showIndicatorControls = true,
  showMarkerToggle = true,
  showMA = true,
  showInfoBar = true,
  visibleBars = 60,
  linkedPrice,
  onDateClick,
  onPriceDoubleClick,
  extColumns,
  addedDate,
}: Props) {
  const [activeIndicators, setActiveIndicators] = useState<string[]>(['vol'])
  const [showMarkers, setShowMarkers] = useState(true)
  const [showEarnings, setShowEarnings] = useState(false)
  const theme = useTheme()
  // 加入自选日标注（与「异动」标记相互独立）
  const [showAddedMark, setShowAddedMark] = useState(true)
  const [volumeCompare, setVolumeCompare] = useState<VolumeCompareConfig>(() =>
    normalizeVolumeCompare(storage.stockVolumeCompare.get(DEFAULT_VOLUME_COMPARE)),
  )
  const dateRange = externalDateRange ?? getDefaultRange()

  // 查询配置统一来自 klineDailyQueryOptions, 与 StockPanel 信息条/邻近预取共享同一 cache key (只发一次请求)
  const kline = useQuery({ ...klineDailyQueryOptions(symbol, dateRange, extColumns), enabled: !!symbol })

  const earnings = useQuery({
    queryKey: QK.earningsBreakout(symbol, dateRange.start, dateRange.end),
    queryFn: () => api.klineEarningsBreakout(symbol, dateRange),
    enabled: showEarnings && !!symbol,
    staleTime: 30_000,
    refetchInterval: showEarnings && dateRange.end >= getDefaultRange().end ? 30_000 : false,
  })
  const report = !earnings.isError && earnings.data?.symbol === symbol ? earnings.data : undefined
  const gapsQuery = useQuery({
    queryKey: QK.klineGaps(symbol, dateRange.end),
    queryFn: () => api.klineGaps(symbol, dateRange.end),
    enabled: !!symbol,
    staleTime: 30_000,
    refetchInterval: dateRange.end >= getDefaultRange().end ? 30_000 : false,
  })
  const gapReport = !gapsQuery.isError && gapsQuery.data?.symbol === symbol ? gapsQuery.data : undefined
  const earningsMarkers = useMemo<ChartMarker[]>(() => showEarnings && report ? [
    ...report.events.map(event => ({ date: event.date, kind: 'neutral' as const, above: true,
      label: '断', color: theme === 'dark' ? '#38BDF8' : '#0284C7' })),
    ...report.markers.map(marker => ({ date: marker.date, kind: 'buy' as const,
      label: marker.icon === 9 ? '缺口' : '中点',
      description: `${marker.icon === 9 ? GAP_RETEST_HINT : MIDPOINT_RETEST_HINT}\n断层日期：${marker.event_date}`,
      color: marker.icon === 9 ? (theme === 'dark' ? '#FACC15' : '#D97706') : (theme === 'dark' ? '#A78BFA' : '#7C3AED'),
      symbol: marker.icon === 9 ? 'arrow' as const : 'diamond' as const, offset: marker.icon === 9 ? 12 : 56 })),
  ] : [], [showEarnings, report, theme])

  const rows = useMemo(() => toOHLC(kline.data?.rows ?? []), [kline.data?.rows])
  const stockInfo = kline.data?.stock_info
  const limitMarkers = useMemo(() => buildLimitUpMarkers(kline.data?.rows ?? []), [kline.data?.rows])
  const allMarkers = useMemo(() => [
    ...(showMarkers ? markers ?? [] : []),
    ...(showMarkers && showLimitMarkers ? limitMarkers : []),
    ...earningsMarkers,
  ], [limitMarkers, markers, showLimitMarkers, showMarkers, earningsMarkers])

  const toggleIndicator = useCallback((key: string) => {
    setActiveIndicators(prev => prev.includes(key) ? prev.filter(k => k !== key) : [...prev, key])
  }, [])

  const updateVolumeCompare = useCallback((patch: Partial<VolumeCompareConfig>) => {
    setVolumeCompare(prev => {
      const next = normalizeVolumeCompare({ ...prev, ...patch })
      storage.stockVolumeCompare.set(next)
      return next
    })
  }, [])

  const activeSubDefs = activeIndicators
    .map(key => SUB_CHARTS.find(s => s.key === key))
    .filter((d): d is typeof SUB_CHARTS[number] => !!d)
  let subExtraH = 0
  activeSubDefs.forEach(def => { subExtraH += SUB_INFO_H + def.height })
  if (activeSubDefs.length > 0) subExtraH += activeSubDefs.length * SUB_GAP + 14
  const chartHeight = height + subExtraH

  if (!symbol) return null

  return (
    <div className={className} style={{ minHeight: chartHeight }}>
      {showIndicatorControls && rows.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 px-1 pb-0.5">
          {SUB_CHARTS.map(ind => (
            <ChartPill
              key={ind.key}
              active={activeIndicators.includes(ind.key)}
              label={ind.label}
              activeClass="bg-accent/20 text-accent"
              onClick={() => toggleIndicator(ind.key)}
            />
          ))}
          {OVERLAY_INDICATORS.map(ind => (
            <ChartPill
              key={ind.key}
              active={activeIndicators.includes(ind.key)}
              label={ind.label}
              activeClass="bg-accent/20 text-accent"
              onClick={() => toggleIndicator(ind.key)}
            />
          ))}
          <ChartPill active={showEarnings} label="业绩断层" activeClass="bg-accent/20 text-accent"
            title="正式财报版：业绩断层、缺口回踩、中点回踩；按当时可用版本计算"
            onClick={() => setShowEarnings(v => !v)} />
          {activeIndicators.includes('vol') && (
            <div className="ml-0.5 flex h-5 items-center gap-1.5 border-l border-border/70 pl-2">
              <span className="text-[10px] text-muted">量比</span>
              <button
                type="button"
                role="switch"
                aria-checked={volumeCompare.enabled}
                aria-label="开启量能对比"
                title={volumeCompare.enabled ? '关闭量能对比' : '开启量能对比'}
                onClick={() => updateVolumeCompare({ enabled: !volumeCompare.enabled })}
                className={`relative h-3.5 w-6 shrink-0 rounded-full transition-colors ${
                  volumeCompare.enabled ? 'bg-accent' : 'bg-elevated'
                }`}
              >
                <span className={`absolute left-0 top-0.5 h-2.5 w-2.5 rounded-full bg-white transition-transform ${
                  volumeCompare.enabled ? 'translate-x-3' : 'translate-x-0.5'
                }`} />
              </button>
              <select
                aria-label="量能对比周期"
                value={volumeCompare.days}
                disabled={!volumeCompare.enabled}
                onChange={event => updateVolumeCompare({ days: Number(event.target.value) })}
                className="h-5 rounded border border-border bg-base px-1 text-[10px] text-secondary outline-none disabled:opacity-40"
              >
                {Array.from({ length: 20 }, (_, index) => index + 1).map(days => (
                  <option key={days} value={days}>前{days}日均量</option>
                ))}
              </select>
            </div>
          )}
          {/* 两个标注开关统一右对齐 */}
          <div className="ml-auto flex items-center gap-1.5">
            {showMarkerToggle && showLimitMarkers && (
              <ChartPill
                active={showMarkers}
                label="异动"
                activeClass="text-[#FACC15] bg-[#FACC15]/10"
                onClick={() => setShowMarkers(v => !v)}
              />
            )}
            {/* 激活色与图表里的 ADDED_DATE_COLOR 一致; Tailwind 只认字面量类名, 故写死色值 */}
            {showMarkerToggle && addedDate && (
              <ChartPill
                active={showAddedMark}
                label="自选"
                activeClass="text-[#3B82F6] bg-[#3B82F6]/10"
                title={showAddedMark ? '隐藏K线上的加入自选日标注' : '显示K线上的加入自选日标注'}
                onClick={() => setShowAddedMark(v => !v)}
              />
            )}
          </div>
        </div>
      )}
      {showEarnings && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-1 py-1 text-[10px] text-muted" role="status">
          <span className="text-sky-600 dark:text-[#38BDF8]">断 · 业绩断层</span>
          <span className="text-amber-600 dark:text-[#FACC15]" title={GAP_RETEST_HINT}>↑ 缺口回踩</span>
          <span className="text-violet-600 dark:text-[#A78BFA]" title={MIDPOINT_RETEST_HINT}>◆ 中点回踩</span>
          {earnings.isLoading && <span>读取财报版本…</span>}
          {earnings.isError && <span className="text-danger">业绩断层读取失败</span>}
          {report && <span>{report.status === 'ready'
            ? `本区间 ${report.events.length} 个断层 · ${report.markers.length} 个回踩信号`
            : ({ no_versions: '暂无财务版本，请在数据页同步财务', data_insufficient: '没有满足条件且当时可用的财报依据，暂不生成信号',
              no_calendar: '缺少本地交易日历', no_prices: '缺少历史日K', unsupported: '仅支持股票正式财报' }[report.status])}
          </span>}
          <span title="首次历史回填沿用财务版本库的保守日期，缺失依据不补信号，后续修订不撤销旧断层">当时可用版本 · 保守历史口径</span>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-1 py-1 text-[10px] text-muted" role="status">
        <span className="text-amber-600 dark:text-[#FACC15]">▰ 向上缺口</span>
        <span className="text-sky-600 dark:text-[#38BDF8]">▰ 向下缺口</span>
        {gapsQuery.isLoading && <span>读取缺口区域…</span>}
        {gapsQuery.isError && <span className="text-danger">缺口区域读取失败</span>}
        {gapReport && <span>{gapReport.status === 'no_prices' ? '缺少历史日K' : `未回补 ${gapReport.gaps.length} 个 · 已存日线 ${gapReport.history_start} 起 · 截止 ${gapReport.as_of}`}</span>}
      </div>
      {kline.isLoading && <div className="text-sm text-muted py-4">加载中…</div>}
      {kline.isError && <div className="text-sm text-danger py-2">日K加载失败</div>}
      {!kline.isLoading && !kline.isError && (kline.data?.rows?.length ?? 0) > 0 && rows.length === 0 && (
        <div className="text-sm text-danger py-2">数据格式异常，请刷新页面</div>
      )}
      {rows.length > 0 && (
        <EChartsCandlestick
          data={rows}
          markers={allMarkers}
          ranges={ranges}
          priceLines={priceLines}
          priceGaps={gapReport?.gaps ?? []}
          height={chartHeight - 22}
          showMA={showMA}
          showInfoBar={showInfoBar}
          showMarkers={true}
          stockInfo={stockInfo}
          symbol={symbol}
          linkedPrice={linkedPrice}
          onDateClick={onDateClick}
          onPriceDoubleClick={onPriceDoubleClick}
          visibleBars={visibleBars}
          activeIndicators={activeIndicators}
          volumeCompare={volumeCompare}
          addedDate={showAddedMark ? addedDate : null}
        />
      )}
    </div>
  )
}
