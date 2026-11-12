import { useExtSeries } from '@/lib/useExtSeries'
import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { useCurrentUser } from '@/lib/auth'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { ScanSearch, Clock, TrendingUp, Star, Filter, Layers, Network, Sparkles, RefreshCw, Settings2, Store, RotateCcw, X, SlidersHorizontal } from 'lucide-react'
import { api, genRuleId, type ScreenerStrategy, type ScreenerResult } from '@/lib/api'
import { fetchMinuteBatchIncremental } from '@/lib/minuteBatchIncremental'
import { DEFAULT_STRATEGY_NOTIFY_EVENTS } from '@/lib/strategyMonitorEvents'
import { toast } from '@/components/Toast'
import { useDataStatus, usePreferences, useCapabilities, useQuoteStatus } from '@/lib/useSharedQueries'
import { useWatchlistBatchAdd } from '@/lib/useSharedMutations'
import { QK } from '@/lib/queryKeys'
import { useScreenerRunStatus } from '@/lib/useScreenerRunStatus'
import { storage } from '@/lib/storage'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { DatePicker } from '@/components/DatePicker'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { type NavItem } from '@/lib/listNav'
import { WatchlistAddMenu } from '@/components/WatchlistAddMenu'
import { useStrategyPool } from '@/lib/useStrategyPool'
import { StrategyCard, CardSize, loadCardSize, cardWrapCls } from '@/components/screener/StrategyCard'
import { ScreenerTable } from '@/components/screener/ScreenerTable'
import { ScreenerFilter as ScreenerFilterType, defaultFilter, filterActive, countActiveFilters, applyFilter, FilterPanel } from '@/components/screener/ScreenerFilter'
import { StrategySettingsDialog } from '@/components/screener/StrategySettingsDialog'
import { DefaultStrategyParamsDialog } from '@/components/screener/DefaultStrategyParamsDialog'
import { StrategyPoolDialog } from '@/components/screener/StrategyPoolDialog'
import { StrategyBuilderDialog } from '@/components/screener/StrategyBuilderDialog'
import { StrategyStoreDialog } from '@/components/screener/StrategyStoreDialog'
import { CompositeStrategyDialog } from '@/components/screener/CompositeStrategyDialog'
import { EarningsGapVerification, EARNINGS_GAP_STRATEGY_ID, useEarningsGapVerification } from '@/components/screener/EarningsGapVerification'
import { ListColumnCustomizer } from '@/components/ListColumnCustomizer'
import { useTableSort } from '@/components/stock-table/useTableSort'
import { resolveCandleConfig } from '@/lib/list-columns'
import {
  SCREENER_BUILTIN_COLUMNS,
  SCREENER_COLUMN_GROUPS,
  buildExtColumnsParam,
  loadScreenerColumnConfig,
  saveScreenerColumnConfig,
  type ColumnConfig,
} from '@/lib/screener-columns'

// 获取策略为占位功能, 暂时隐藏入口; 恢复时改回 true
const SHOW_STRATEGY_STORE = false

export function Screener() {
  const isAdmin = useCurrentUser()?.role === 'admin'
  const [assetType, setAssetType] = useState<'stock' | 'etf'>('stock')
  // 周期显示筛选: 全部 / 日线 / 分钟 — 只过滤卡片显示, 不影响池和执行;
  // 执行按每个策略自己声明的 timeframes 路由 (日线走盘后缓存, 分钟走本地分钟K分区)
  const [tfFilter, setTfFilter] = useState<'all' | '1d' | '1m'>('all')
  const [activeStrategy, setActiveStrategy] = useState<string | null>(null)
  const [result, setResult] = useState<ScreenerResult | null>(null)
  const [asOf, setAsOf] = useState<string>('')
  const [batchMsg, setBatchMsg] = useState<string>('')
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)
  const [previewName, setPreviewName] = useState<string>('')
  const [previewNavList, setPreviewNavList] = useState<NavItem[]>([])
  const closePreview = useCallback(() => {
    setPreviewSymbol(null)
    setPreviewName('')
    setPreviewNavList([])
  }, [])
  const [settingsStrategyId, setSettingsStrategyId] = useState<string | null>(null)
  const [showPoolDialog, setShowPoolDialog] = useState(false)
  const [showBuilder, setShowBuilder] = useState(false)
  const [builderMode, setBuilderMode] = useState<'create' | 'modify'>('create')
  const [showStore, setShowStore] = useState(false)
  const [showDefaultParams, setShowDefaultParams] = useState(false)
  const [showComposite, setShowComposite] = useState(false)
  const { pool, addToPool, removeFromPool, reorderPool, prune } = useStrategyPool()
  const [cardSize, setCardSize] = useState<CardSize>(loadCardSize)
  // 日k蜡烛图显示开关（仅当 candle 列可见时才有意义；持久化）
  const [dailyKChartVisible, setDailyKChartVisible] = useState<boolean>(() => storage.screenerCandle.get(true))
  const toggleDailyKChart = useCallback(() => {
    setDailyKChartVisible(v => {
      const next = !v
      storage.screenerCandle.set(next)
      return next
    })
  }, [])
  // 分时图显示开关（仅当 intraday 列可见时才有意义；持久化）
  const [intradayChartVisible, setIntradayChartVisible] = useState<boolean>(() => storage.screenerIntraday.get(true))
  const toggleIntradayChart = useCallback(() => {
    setIntradayChartVisible(v => {
      const next = !v
      storage.screenerIntraday.set(next)
      return next
    })
  }, [])
  // 策略列标签全表展开/收起（命中多策略时行会很高；默认收起, 每行可单独展开；持久化）
  const [strategyTagsExpanded, setStrategyTagsExpanded] = useState<boolean>(() => storage.screenerStrategyTags.get(false))
  const toggleStrategyTags = useCallback(() => {
    setStrategyTagsExpanded(v => {
      const next = !v
      storage.screenerStrategyTags.set(next)
      return next
    })
  }, [])
  // 截断提示可关闭 (仅本次会话, 不持久化)
  const [intradayCapDismissed, setIntradayCapDismissed] = useState(false)
  const [showAll, setShowAll] = useState(false)
  const [showFilter, setShowFilter] = useState(false)
  const [filter, setFilter] = useState<ScreenerFilterType>(defaultFilter)
  const filterMap = useRef<Map<string, ScreenerFilterType>>(new Map())
  const qc = useQueryClient()

  // 结果列配置 — 默认内置列，异步合并后端/localStorage 偏好
  const [columns, setColumns] = useState<ColumnConfig[]>([...SCREENER_BUILTIN_COLUMNS])
  const [customizerOpen, setCustomizerOpen] = useState(false)
  const columnsLoaded = useRef(false)

  useEffect(() => {
    if (columnsLoaded.current) return
    columnsLoaded.current = true
    loadScreenerColumnConfig().then(setColumns)
  }, [])

  const handleColumnsChange = useCallback((next: ColumnConfig[]) => {
    setColumns(next)
    saveScreenerColumnConfig(next)
  }, [])

  const extColumnsParam = useMemo(() => buildExtColumnsParam(columns), [columns])

  // 各策略上次手动计算的命中数
  const [hitCounts, setHitCounts] = useState<Record<string, number>>({})
  // 各策略失效数 (今日曾命中 - 当前命中)
  const [expiredCounts, setExpiredCounts] = useState<Record<string, number>>({})
  // 各策略显示上限 (null = 全部)
  const [strategyLimits, setStrategyLimits] = useState<Record<string, number | null>>({})
  const runStatus = useScreenerRunStatus(assetType, asOf)
  const pendingRunIds = useMemo(() => new Set(runStatus.pendingIds), [runStatus.pendingIds])

  // 筛选条件变化时同步到 map（供切换策略时读取最新值）
  useEffect(() => {
    if (activeStrategy) filterMap.current.set(activeStrategy, filter)
  }, [filter, activeStrategy])

  // 切换策略时恢复该策略之前保存的筛选
  const handleStrategySwitch = useCallback((strategyId: string) => {
    setFilter(filterMap.current.get(strategyId) ?? { ...defaultFilter })
  }, [])

  // 对原始结果应用过滤 (memo: 否则每次渲染都对全部结果行过滤,
  // 且新数组身份会击穿下游 displayRows 的 memo)
  const filteredRows = useMemo(
    () => (result ? applyFilter(result.rows, filter) : []),
    [result, filter],
  )

  const { data: prefs } = usePreferences()

  // 统一列表: 不按周期过滤, 日线+分钟策略合并返回, 分钟策略带 timeframes 标识
  const strategies = useQuery({
    queryKey: [...QK.screenerStrategies('all'), 'all'],
    queryFn: () => api.screenerStrategies(undefined, 'all'),
    staleTime: 60_000,  // 策略池清单慢变 (与 Paper 同款), 减少切页重拉
  })

  // 查询只读快照，页面加载、日期切换与行情更新均不提交计算。
  const summaryQuery = runStatus.summary
  const fullCachedQuery = useQuery({
    queryKey: QK.screenerSnapshot(assetType, 'all', asOf, extColumnsParam),
    queryFn: () => api.screenerSnapshots(assetType, asOf || undefined, extColumnsParam || undefined),
    enabled: showAll,
  })
  const singleCachedQuery = useQuery({
    queryKey: QK.screenerSnapshot(assetType, activeStrategy ?? '', asOf, extColumnsParam),
    queryFn: () => api.screenerSnapshot(activeStrategy!, assetType, asOf || undefined, extColumnsParam || undefined),
    enabled: !showAll && !!activeStrategy,
  })

  const dataStatus = useDataStatus({ staleTime: 0 })

  const gapResult = result?.strategy === EARNINGS_GAP_STRATEGY_ID && result.as_of === asOf
    ? result
    : singleCachedQuery.data?.result?.strategy === EARNINGS_GAP_STRATEGY_ID && singleCachedQuery.data.result.as_of === asOf
      ? singleCachedQuery.data.result
      : null
  const gapSummary = summaryQuery.data?.results[EARNINGS_GAP_STRATEGY_ID]
  const gapVerification = gapResult?.verification ?? (gapSummary?.as_of === asOf ? gapSummary.verification : undefined)
  const gapControl = useEarningsGapVerification({
    asOf, latestDate: dataStatus.data?.enriched?.latest_date ?? null,
    assetType, verification: gapVerification,
  })

  // 默认查询最新数据日期；只改变浏览范围，不触发计算。
  useEffect(() => {
    const latest = dataStatus.data?.enriched?.latest_date
    if (latest) setAsOf(latest)
  }, [dataStatus.data?.enriched?.latest_date])

  const strategyPresets = useMemo(
    () => (strategies.data?.presets ?? []).filter(s => s.asset_types.includes(assetType)),
    [strategies.data, assetType],
  )

  // 策略 ID → 名称映射
  const strategyIdToName = useMemo(() => {
    const map: Record<string, string> = {}
    for (const p of strategyPresets) {
      map[p.id] = p.name
    }
    return map
  }, [strategyPresets])

  // 策略 ID → 完整对象映射（避免每张卡片 find 遍历）
  const strategyMap = useMemo(() => {
    const map = new Map<string, ScreenerStrategy>()
    for (const p of strategyPresets) {
      map.set(p.id, p)
    }
    return map
  }, [strategyPresets])

  const allStrategyIds = useMemo(
    () => new Set((strategies.data?.presets ?? []).map(s => s.id)),
    [strategies.data],
  )
  const availableStrategyIds = useMemo(
    () => new Set(strategyPresets.map(s => s.id)),
    [strategyPresets],
  )
  const visiblePool = useMemo(() => pool.filter(id => availableStrategyIds.has(id)), [pool, availableStrategyIds])

  // 卡片显示: 按周期筛选 (all=全部, 1d=仅日线, 1m=仅分钟); 未声明 timeframes 视为日线
  const displayPool = useMemo(() => visiblePool.filter(id => {
    if (tfFilter === 'all') return true
    const isMinute = strategyMap.get(id)?.timeframes?.includes('1m') ?? false
    return tfFilter === '1m' ? isMinute : !isMinute
  }), [visiblePool, strategyMap, tfFilter])

  // 策略列表加载后,自动清除池中失效的自定义策略(如本地开发残留的、
  // 当前后端已不存在的策略 ID),避免"策略池"对话框持续显示失效项。
  // 关键: 仅当本次拉取成功且返回非空列表时才 prune。
  // 拉取中/失败/返回空(如引擎 reload 瞬时把某策略跳过)时一律不碰池,
  // 否则会把用户池里仍有效的 ID 永久清空并写入 localStorage,导致卡片全没。
  // 日线/分钟池按周期隔离, 各自用自身周期的列表清理, 互不影响。
  useEffect(() => {
    if (strategies.isError) return        // 拉取失败: 不 prune
    if (!strategies.isSuccess) return     // 加载中: 不 prune
    if (allStrategyIds.size === 0) return  // 空列表: 不 prune
    prune(allStrategyIds)
  }, [allStrategyIds, prune, strategies.isError, strategies.isSuccess])

  // 策略文件加载失败时提示用户(避免"策略静默消失"被误判为正常)
  const loadErrors = strategies.data?.load_errors ?? []
  useEffect(() => {
    for (const e of loadErrors) {
      toast(`策略「${e.file}」加载失败：${e.error}`, 'error')
    }
  }, [loadErrors])

  useEffect(() => {
    const counts: Record<string, number> = {}
    for (const [id, item] of Object.entries(summaryQuery.data?.results ?? {})) counts[id] = item.total
    setHitCounts(counts)
    setExpiredCounts({})
  }, [summaryQuery.data])

  useEffect(() => {
    const cached = singleCachedQuery.data?.result
    if (!showAll && cached?.strategy === activeStrategy) setResult(cached)
  }, [singleCachedQuery.data, showAll, activeStrategy])

  const effectiveResults = fullCachedQuery.data?.results ?? null

  // symbol → 所属策略列表。单策略接口同时返回轻量归属映射，保留策略列原有展示。
  const symbolStrategyMap = useMemo(() => {
    const map = new Map<string, string[]>()
    if (showAll) {
      for (const [sid, r] of Object.entries(effectiveResults ?? {})) {
        for (const row of r.rows) {
          const arr = map.get(row.symbol)
          if (arr) arr.push(sid)
          else map.set(row.symbol, [sid])
        }
      }
      return map
    }
    for (const [symbol, ids] of Object.entries(singleCachedQuery.data?.strategy_ids_by_symbol ?? {})) {
      map.set(symbol, ids)
    }
    if (activeStrategy && result) {
      for (const row of result.rows) {
        if (!map.has(row.symbol)) map.set(row.symbol, [activeStrategy])
      }
    }
    return map
  }, [showAll, effectiveResults, singleCachedQuery.data, activeStrategy, result])

  // "全部" 模式: 合并所有策略的去重个股
  const allRows = useMemo(() => {
    if (!effectiveResults) return []
    const seen = new Set<string>()
    const merged: any[] = []
    for (const r of Object.values(effectiveResults)) {
      for (const row of r.rows) {
        if (!seen.has(row.symbol)) {
          seen.add(row.symbol)
          merged.push(row)
        }
      }
    }
    return merged
  }, [effectiveResults])

  // 计算当前策略的失效行: 今日曾命中但当前已不命中。
  const expiredRows = useMemo(() => {
    const everRows = singleCachedQuery.data?.today_ever_rows
    if (!everRows || !result || result.as_of !== asOf) return []
    const currentSymbols = new Set(result.rows.map((row: any) => row.symbol))
    return Object.entries(everRows)
      .filter(([symbol]) => !currentSymbols.has(symbol))
      .map(([, row]) => ({ ...row, _expired: true }))
  }, [singleCachedQuery.data, result, asOf])

  // 表头排序（受控）：用户点击列则按该列；未点时下方按评分默认降序
  const { sort, toggle, sortRows } = useTableSort()

  // 当前显示的行数据 (全部模式 或 单策略模式) + 失效行
  const displayRows = useMemo(() => {
    let rows = showAll
      ? applyFilter(allRows, filter)
      : filteredRows
    // 排序：用户点了表头则按该列，否则默认评分降序
    rows = sort
      ? sortRows(rows, columns)
      : [...rows].sort((a, b) => (b.score ?? -Infinity) - (a.score ?? -Infinity))
    const limit = !showAll && activeStrategy
      ? strategyLimits[activeStrategy] ?? null
      : null
    const mainRows = limit != null ? rows.slice(0, limit) : rows

    // 追加当前策略的失效行 (灰色)
    if (!showAll && activeStrategy) {
      if (expiredRows.length > 0) {
        return [...mainRows, ...expiredRows]
      }
    }
    return mainRows
  }, [showAll, allRows, filteredRows, filter, activeStrategy, strategyLimits, expiredRows, sort, sortRows, columns])

  // 日k列是否启用 → 决定是否加载批量 kline 数据
  const candleColumn = useMemo(() =>
    columns.find(c => c.source.type === 'builtin' && c.source.key === 'candle' && c.visible),
    [columns],
  )
  const candleColumnEnabled = !!candleColumn
  // 日k天数（来自列配置，已钳制边界）
  const candleDays = useMemo(() => resolveCandleConfig(candleColumn?.candleConfig).days, [candleColumn])
  // 真正请求/渲染蜡烛图：列可见 且 眼睛开关开启
  const dailyKVisible = candleColumnEnabled && dailyKChartVisible

  // 批量日k数据 (仅当蜡烛图可见时加载，省请求)
  const dailyKSymbols = useMemo(
    () => [...new Set(displayRows.map((r: any) => r.symbol as string))].sort(),
    [displayRows],
  )
  const resultSymbolsKey = dailyKSymbols.join(',')
  const klineBatch = useQuery({
    queryKey: QK.screenerKlineBatch(`${resultSymbolsKey}|${candleDays}`),
    queryFn: ({ signal }) => api.klineDailyBatch(dailyKSymbols, candleDays, signal),
    enabled: dailyKVisible && dailyKSymbols.length > 0,
    staleTime: 5 * 60_000,
    placeholderData: previousData => previousData,
  })
  const klineData = dailyKVisible ? (klineBatch.data?.data ?? {}) : {}

  // 分时列是否启用 → 决定是否加载批量分时数据 (需 kline.minute.batch 能力)
  const intradayColumn = useMemo(() =>
    columns.find(c => c.source.type === 'builtin' && c.source.key === 'intraday' && c.visible),
    [columns],
  )
  // 分时图依赖分钟K批量数据 (kline.minute.batch), 无数据时开了列也不拉
  const caps = useCapabilities()
  // 全量分钟服务健康 (freshness 契约): 健康时本地分区按配置间隔持续落盘,
  // 分时读本地不受批量上限约束 → 不截断 + prefer_local; 与监控设置页共享缓存
  const refreshStatus = useQuery({
    queryKey: ['minute-refresh-status'],
    queryFn: api.minuteRefreshStatus,
    refetchInterval: 15000,
  })
  const fullMinuteHealthy = !!refreshStatus.data?.healthy
  const hasMinuteBatch = !!caps.data?.capabilities?.['kline.minute.batch']
  const intradayVisible = !!intradayColumn && hasMinuteBatch && intradayChartVisible

  // 分时数据加载策略 (与自选页一致, 简洁优先):
  //  - 全量加载当前列表 symbol, 但按数据源 batch 上限截断,
  //    超出时只取前 batch 只并提示用户, 避免一次性发太多请求打爆 rpm 配额
  //  - 刷新: minute_intraday_refresh 偏好开启时按用户设定间隔轮询; 否则仅首次加载,
  //    用户可点表头刷新按钮手动更新
  const minuteBatchCap = caps.data?.capabilities?.['kline.minute.batch']?.batch ?? 100
  const quoteStatus = useQuoteStatus()
  const realtimeRunning = quoteStatus.data?.running ?? false
  const intradayRefreshEnabled = prefs?.minute_intraday_refresh ?? false
  const intradayRefreshInterval = prefs?.minute_intraday_refresh_interval ?? 6

  const allIntradaySymbols = useMemo(
    () => displayRows.map((r: any) => r.symbol),
    [displayRows],
  )
  // 拉模型 (走批量接口) 才截断到 batch 上限; 全量分钟健康时读本地分区无上限
  const intradayTruncated = intradayVisible && !fullMinuteHealthy && allIntradaySymbols.length > minuteBatchCap
  const intradaySymbols = useMemo(
    () => intradayTruncated ? allIntradaySymbols.slice(0, minuteBatchCap) : allIntradaySymbols,
    [allIntradaySymbols, intradayTruncated, minuteBatchCap, fullMinuteHealthy],
  )
  const intradayRequestSymbols = useMemo(
    () => [...new Set(intradaySymbols)].sort(),
    [intradaySymbols],
  )
  const intradaySymbolsKey = intradayRequestSymbols.join(',')

  const minuteBatch = useQuery({
    queryKey: QK.minuteBatch(intradaySymbolsKey),
    // 增量轮询: 读缓存以最后一根为 since 只拉新增, 本地合并为完整序列
    queryFn: () => fetchMinuteBatchIncremental(qc, QK.minuteBatch(intradaySymbolsKey), intradayRequestSymbols, fullMinuteHealthy),
    enabled: intradayVisible && intradayRequestSymbols.length > 0,
    staleTime: 10_000,
    placeholderData: previousData => previousData,
    // 仅当开启分时刷新偏好 且 盘中实时行情运行时 才轮询 (省 rpm)
    refetchInterval: (intradayRefreshEnabled && realtimeRunning) ? intradayRefreshInterval * 1000 : false,
  })
  const minuteData = intradayVisible ? (minuteBatch.data?.data ?? {}) : {}

  const handleRun = (strategy: ScreenerStrategy) => {
    handleStrategySwitch(strategy.id)
    setActiveStrategy(strategy.id)
    setShowAll(false)
    if (result?.strategy !== strategy.id) setResult(null)
    void runStatus.submit(strategy.id)
  }

  const handleDateChange = (newDate: string) => {
    setAsOf(newDate)
    setResult(null)
  }

  const minDate = dataStatus.data?.enriched?.earliest_date ?? ''
  const maxDate = dataStatus.data?.enriched?.latest_date ?? ''

  const batchAdd = useWatchlistBatchAdd()

  // 自选股列表 (用于判断是否在自选中)
  const watchlist = useQuery({
    queryKey: QK.watchlist,
    queryFn: api.watchlistList,
    staleTime: 30_000,
  })
  const watchlistSet = useMemo(() => {
    const symbols = watchlist.data?.symbols ?? []
    return new Set(symbols.map((s: any) => s.symbol))
  }, [watchlist.data])

  // 单只股票加入/移出自选
  const toggleWatchlist = useMutation({
    mutationFn: ({
      symbol,
      action,
      groupId,
    }: {
      symbol: string
      action: 'add' | 'remove'
      groupId?: string | null
    }) => action === 'remove'
      ? api.watchlistRemove(symbol)
      : api.watchlistAdd(symbol, '', groupId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.watchlist })
      qc.invalidateQueries({ queryKey: ['watchlist-enriched'] })
    },
  })

  const reloadStrategies = useMutation({
    mutationFn: api.strategyReload,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['screener-strategies'] })
      void qc.invalidateQueries({ queryKey: QK.screenerTasksRoot })
    },
  })

  const extSeries = useExtSeries(columns, displayRows.map((row: { symbol: string }) => row.symbol), asOf)

  // 策略监控: 查询规则, 建立 assetType:strategyId → ruleId 映射 (只看 type=strategy 且 enabled)
  const monitorRules = useQuery({ queryKey: QK.monitorRules, queryFn: api.monitorRulesList, enabled: isAdmin })
  const strategyMonitorMap = useMemo(() => {
    const m = new Map<string, string>()
    for (const r of monitorRules.data?.rules ?? []) {
      if (r.type === 'strategy' && r.enabled && r.strategy_id) {
        m.set(`${r.asset_type ?? 'stock'}:${r.strategy_id}`, r.id)
      }
    }
    return m
  }, [monitorRules.data])

  const toggleStrategyMonitor = (strategyId: string, strategyName: string) => {
    const existingRuleId = strategyMonitorMap.get(`${assetType}:${strategyId}`)
    if (existingRuleId) {
      // 已监控 → 删除规则
      api.monitorRuleDelete(existingRuleId).then(() =>
        qc.invalidateQueries({ queryKey: QK.monitorRules }),
      )
    } else {
      // 未监控 → 直接创建 type=strategy 规则; 必须带当前页资产类型,
      // 否则 ETF 页点监控会默认 stock, 引擎在股票轮评估, ETF 命中永不告警。
      api.monitorRuleSave({
        id: genRuleId(),
        name: `策略监控 · ${strategyName}`,
        enabled: true,
        type: 'strategy',
        asset_type: assetType,
        scope: 'all',
        symbols: [],
        sector: null,
        strategy_id: strategyId,
        direction: 'entry',
        notify_events: [...DEFAULT_STRATEGY_NOTIFY_EVENTS],
        conditions: [],
        logic: 'or',
        cooldown_seconds: 3600,
        severity: 'info',
        message: '',
      }).then(() => qc.invalidateQueries({ queryKey: QK.monitorRules }))
    }
  }

  const handleBatchAdd = (groupId: string | null) => {
    if (!displayRows.length) return
    const symbols = displayRows.map((r: any) => r.symbol)
    batchAdd.mutate({ symbols, groupId }, {
      onSuccess: (data) => {
        setBatchMsg(`已添加 ${data.added} 只到自选`)
        setTimeout(() => setBatchMsg(''), 3000)
      },
      onError: () => {
        setBatchMsg('添加失败')
        setTimeout(() => setBatchMsg(''), 3000)
      },
    })
  }


  return (
    <>
      <PageHeader
        title="策略"
        subtitle="每天 16:00 后点击更新 · 有效结果直接复用"
        right={
          <div className="flex items-center gap-2 flex-nowrap overflow-x-auto max-w-full [&>*]:shrink-0 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
            {/* 资产类型切换: 股票 / ETF (分钟策略 asset_types 仅股票, ETF 列表自然不含) */}
            <div className="flex items-center h-7 rounded-btn border border-border overflow-hidden">
              {(['stock', 'etf'] as const).map(t => (
                <button
                  key={t}
                  onClick={() => { setAssetType(t); setActiveStrategy(null); setResult(null); setShowAll(false) }}
                  className={`h-full px-2.5 text-xs font-medium transition-colors
                    cursor-pointer ${assetType === t
                      ? 'bg-accent/10 text-accent'
                      : 'text-muted hover:text-secondary hover:bg-elevated'
                    }`}
                >
                  {t === 'stock' ? '股票' : 'ETF'}
                </button>
              ))}
            </div>
            {/* 周期筛选: 全部 / 日线 / 分钟 — 只过滤卡片显示, 不影响池与执行路由 */}
            <div className="flex items-center h-7 rounded-btn border border-border overflow-hidden">
              {(['all', '1d', '1m'] as const).map(tf => (
                <button
                  key={tf}
                  onClick={() => {
                    if (tfFilter === tf) return
                    setTfFilter(tf)
                    setActiveStrategy(null); setResult(null); setShowAll(false)
                  }}
                  className={`h-full px-2.5 text-xs font-medium transition-colors cursor-pointer
                    ${tfFilter === tf
                      ? 'bg-accent/10 text-accent'
                      : 'text-muted hover:text-secondary hover:bg-elevated'
                    }`}
                >
                  {tf === 'all' ? '全部' : tf === '1d' ? '日线' : '分钟'}
                </button>
              ))}
            </div>
            {/* 重载策略定义；计算仍由点击标签触发 */}
            <button
              onClick={() => reloadStrategies.mutate()}
              disabled={reloadStrategies.isPending || runStatus.running}
              title="重载策略定义，点击标签检查结果更新"
              className="inline-flex items-center gap-1.5 h-7 px-2.5 rounded-btn
                border border-border bg-surface text-xs font-medium text-muted
                hover:text-accent hover:border-accent/50 transition-colors cursor-pointer
                disabled:opacity-50 disabled:cursor-wait"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${reloadStrategies.isPending ? 'animate-spin' : ''}`} />
              重载
            </button>
            {asOf && (
              <DatePicker
                value={asOf}
                onChange={handleDateChange}
                min={minDate}
                max={maxDate}
              />
            )}
            {/* 全部切换 */}
            <button
              onClick={() => setShowAll(v => { if (!v) setActiveStrategy(null); return !v })}
              title="显示全部策略个股"
              className={`inline-flex items-center justify-center h-7 w-7 rounded-btn border transition-colors cursor-pointer
                ${showAll
                  ? 'border-accent/50 bg-accent/10 text-accent'
                  : 'border-border bg-surface text-muted hover:text-secondary hover:border-accent/40'
                }`}
            >
              <Network className="h-3.5 w-3.5" />
            </button>
            {/* 卡片尺寸切换 */}
            <div className="flex items-center h-7 rounded-btn border border-border overflow-hidden">
              {(['hidden', 'mini', 'normal', 'large'] as const).map(sz => (
                <button
                  key={sz}
                  onClick={() => { setCardSize(sz); storage.screenerCardSize.set(sz) }}
                  className={`h-full px-2 text-[10px] font-medium transition-colors cursor-pointer
                    ${cardSize === sz
                      ? 'bg-accent/10 text-accent'
                      : 'text-muted hover:text-secondary hover:bg-elevated'
                    }`}
                >
                  {sz === 'hidden' ? '隐藏' : sz === 'mini' ? '紧凑' : sz === 'normal' ? '标准' : '详细'}
                </button>
              ))}
            </div>
            {/* 策略池按钮 */}
            <button
              onClick={() => setShowPoolDialog(true)}
              className="inline-flex items-center gap-1.5 h-7 px-3 rounded-btn
                border border-border bg-surface text-xs font-medium text-secondary
                hover:text-accent hover:border-accent/50 transition-colors cursor-pointer"
            >
              <Layers className="h-3.5 w-3.5" />
              策略池
              <span className="ml-0.5 min-w-[28px] h-4 flex items-center justify-center rounded-full bg-accent/15 text-accent text-[10px] font-bold">
                {visiblePool.length}/{strategyPresets.length}
              </span>
            </button>
            {/* 创建叠加策略 */}
            <button
              onClick={() => setShowComposite(true)}
              className="inline-flex items-center gap-1.5 h-7 px-3 rounded-btn
                text-xs font-medium text-teal-400 border border-teal-500/20 bg-teal-500/5
                hover:bg-teal-500/15 transition-colors cursor-pointer"
            >
              <Layers className="h-3.5 w-3.5" />
              叠加策略
            </button>
            {/* 创建策略 */}
            <button
              disabled title="Python 策略由服务器维护者部署" onClick={() => { setBuilderMode('create'); setShowBuilder(true) }}
              className="inline-flex items-center gap-1.5 h-7 px-3 rounded-btn
                text-xs font-medium text-amber-400 border border-amber-400/20 bg-amber-400/5
                hover:bg-amber-400/15 transition-colors cursor-pointer"
            >
              <Sparkles className="h-3.5 w-3.5" />
              Python 策略仅服务器部署
            </button>
            {/* 获取策略（占位，敬请期待）— 暂时隐藏 */}
            {SHOW_STRATEGY_STORE && (
              <button
                onClick={() => setShowStore(true)}
                className="inline-flex items-center gap-1.5 h-7 px-3 rounded-btn
                  border border-border bg-surface text-xs font-medium text-secondary
                  hover:text-accent hover:border-accent/50 transition-colors cursor-pointer"
              >
                <Store className="h-3.5 w-3.5" />
                获取策略
              </button>
            )}
            {/* 默认基础参数: 之后新建策略默认使用的基础过滤配置 */}
            <button
              onClick={() => setShowDefaultParams(true)}
              title="默认基础参数 — 新建策略默认使用的基础过滤配置"
              className="inline-flex items-center justify-center h-7 w-7 rounded-btn
                border border-border bg-surface text-muted
                hover:text-accent hover:border-accent/50 transition-colors cursor-pointer"
            >
              <SlidersHorizontal className="h-3.5 w-3.5" />
            </button>
          </div>
        }
      />

      <div className="px-8 py-4 space-y-3">
        {runStatus.connectionNotice && <p role="status" className="text-sm text-warning">{runStatus.connectionNotice}
          <button className="ml-2 text-accent" onClick={() => void summaryQuery.refetch()}>重试查询</button>
        </p>}
        {activeStrategy && (runStatus.errors[activeStrategy] || runStatus.notices[activeStrategy]) && (
          <p role={runStatus.errors[activeStrategy] ? 'alert' : 'status'} className="text-sm text-secondary">
            {runStatus.errors[activeStrategy] || runStatus.notices[activeStrategy]}
          </p>
        )}
        {activeStrategy && runStatus.tasks[activeStrategy] && !runStatus.tasks[activeStrategy].done && (
          <div className="flex items-center gap-3 text-xs text-muted">
            <span>{({ queued: '正在排队', waiting: '等待计算资源', running: '正在计算', cancelling: '正在取消' } as Record<string, string>)[runStatus.tasks[activeStrategy].phase ?? 'queued']}，完成后显示新结果。</span>
            <button className="text-accent disabled:opacity-50" disabled={runStatus.tasks[activeStrategy].phase === 'cancelling'}
              onClick={() => void runStatus.cancel(activeStrategy)}>取消此策略计算</button>
          </div>
        )}
        {/* 策略卡片 */}
        {cardSize !== 'hidden' && (
        <section>
          {strategies.isLoading && <div className="text-sm text-muted">加载中…</div>}
          {!strategies.isLoading && strategies.isError && (
            <div className="flex flex-col items-center gap-2 py-4 text-center border border-dashed border-danger/40 rounded-btn">
              <span className="text-sm text-secondary">策略列表加载失败，请重试；策略池配置本身不受影响。</span>
              <button
                type="button"
                onClick={() => strategies.refetch()}
                disabled={strategies.isFetching}
                className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs text-accent transition-colors hover:bg-elevated disabled:opacity-50"
              >
                重试
              </button>
            </div>
          )}
          {!strategies.isLoading && !strategies.isError && displayPool.length === 0 && (
            <div className="text-sm text-muted py-4 text-center border border-dashed border-border rounded-btn">
              {pool.length === 0
                ? '策略池为空，点击右上角「策略池」按钮添加策略'
                : '当前周期筛选下无策略，切换周期筛选或编辑策略池'}
            </div>
          )}
          <div className={cardWrapCls(cardSize)}>
            {displayPool.map(id => {
              const s = strategyMap.get(id)
              if (!s) return null
              const isMinute = s.timeframes?.includes('1m') ?? false
              return (
                <StrategyCard
                  key={s.id}
                  name={s.name}
                  description={s.description}
                  source={s.source}
                  active={activeStrategy === s.id}
                  count={hitCounts[id]}
                  expiredCount={expiredCounts[id]}
                  loading={false}
                  computing={pendingRunIds.has(id) || (s.id === EARNINGS_GAP_STRATEGY_ID && gapControl.busy)}
                  awaitRun={hitCounts[id] == null}
                  cardSize={cardSize}
                  onRun={() => handleRun(s)}
                  resultDate={summaryQuery.data?.results[id]?.as_of ?? undefined}
                  onSettings={() => setSettingsStrategyId(s.id)}
                  monitored={strategyMonitorMap.has(`${assetType}:${s.id}`)}
                  onToggleMonitor={isAdmin && s.realtime_supported !== false ? () => toggleStrategyMonitor(s.id, s.name) : undefined}
                  timeframeBadge={isMinute ? '分钟' : undefined}
                />
              )
            })}
          </div>
        </section>
        )}

        {/* 结果 */}
        <section>
          {!showAll && assetType === 'stock' && activeStrategy === EARNINGS_GAP_STRATEGY_ID && (
            <div className="mb-4">
              <EarningsGapVerification verification={gapVerification} job={gapControl.job}
                submitting={gapControl.busy} canVerify={gapControl.canVerify} historical={gapControl.historical} error={gapControl.error}
                onVerify={gapControl.start} />
            </div>
          )}


          {(showAll ? allRows.length > 0 : !!result) && (
            <motion.div
              key={showAll ? `all-${asOf}` : `${result!.as_of}-${result!.strategy}`}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
              className="space-y-3"
            >
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-medium text-foreground flex items-center gap-2">
                  {!showAll && activeStrategy && (
                    <span className="text-secondary">{strategyIdToName[activeStrategy] ?? ''}{result?.as_of && <span className="ml-2 text-muted text-xs">结果日期 {result.as_of}</span>}</span>
                  )}
                  <TrendingUp className="h-4 w-4 text-accent" />
                  {showAll ? '全部' : ''}命中 <span className="text-accent num">{displayRows.length}</span> 只
                  {filterActive(filter) && displayRows.length !== (showAll ? allRows.length : result!.total) && (
                    <span className="text-muted text-xs">/ {showAll ? allRows.length : result!.total}</span>
                  )}
                  <span className="text-[11px] text-muted font-normal">
                    · {displayPool.length} 策略
                    {!showAll && displayPool.length > 0 && (
                      <> · 共 {displayPool.reduce((sum, id) => sum + (hitCounts[id] ?? 0), 0)} 只</>
                    )}
                  </span>
                </h2>
                <div className="flex items-center gap-3">
                  {(showAll ? allRows.length > 0 : !!result?.rows.length) && (
                    <div className="inline-flex items-stretch h-7 rounded-btn border border-border bg-surface overflow-hidden">
                      <button
                        onClick={() => setShowFilter(v => !v)}
                        className={`inline-flex items-center gap-1.5 px-2.5 text-xs font-medium transition-colors duration-150 cursor-pointer
                          ${filterActive(filter)
                            ? 'bg-accent/15 text-accent'
                            : showFilter
                              ? 'bg-accent/8 text-accent'
                              : 'text-secondary hover:bg-elevated hover:text-foreground'
                          }`}
                      >
                        <Filter className="h-3 w-3" />
                        筛选
                        {filterActive(filter) && (
                          <span className="bg-accent text-white rounded-full min-w-4 h-4 px-1 flex items-center justify-center text-[10px] font-bold leading-none">
                            {countActiveFilters(filter)}
                          </span>
                        )}
                      </button>
                      {filterActive(filter) && (
                        <>
                          <span className="w-px self-stretch my-1 bg-border" />
                          <button
                            onClick={() => {
                              setFilter(defaultFilter)
                              if (activeStrategy) filterMap.current.delete(activeStrategy)
                            }}
                            title="清空筛选条件"
                            className="inline-flex items-center gap-1 px-2 text-muted
                              hover:bg-danger/10 hover:text-danger transition-colors duration-150 cursor-pointer"
                          >
                            <RotateCcw className="h-3 w-3" />
                          </button>
                        </>
                      )}
                    </div>
                  )}
                  {displayRows.length > 0 && (
                    <WatchlistAddMenu
                      onSelect={handleBatchAdd}
                      disabled={batchAdd.isPending}
                      align="right"
                      title="批量加自选"
                      ariaLabel="批量加入自选"
                      triggerClassName="inline-flex items-center gap-1.5 h-7 px-2.5 rounded-btn
                        border border-accent/40 bg-accent/10 text-accent text-xs font-medium
                        hover:bg-accent/20 disabled:opacity-50 transition-colors duration-150 cursor-pointer"
                    >
                      <Star className="h-3 w-3" />
                      {batchAdd.isPending ? '添加中…' : '批量加自选'}
                    </WatchlistAddMenu>
                  )}
                  <button
                    onClick={() => setCustomizerOpen(true)}
                    title="列表配置"
                    className={`inline-flex items-center justify-center h-7 w-7 rounded-btn border text-xs font-medium transition-colors cursor-pointer
                      ${customizerOpen
                        ? 'border-accent/50 bg-accent/10 text-accent'
                        : 'border-border bg-surface text-secondary hover:text-accent hover:border-accent/50'
                      }`}
                  >
                    <Settings2 className="h-3 w-3" />
                  </button>
                  {batchMsg && (
                    <span className="text-xs text-accent animate-pulse">{batchMsg}</span>
                  )}
                  {!showAll && result && result.elapsed_ms > 0 && (
                    <div className="flex items-center gap-2 text-xs text-muted">
                      <Clock className="h-3 w-3" />
                      <span className="num">{result.elapsed_ms.toFixed(1)} ms</span>
                    </div>
                  )}
                  {/* 分时截断提示: 超数据源批量上限时在工具栏内联显示, 可关闭 */}
                  {intradayTruncated && !intradayCapDismissed && (
                    <span className="inline-flex items-center gap-1 text-xs text-warning/90">
                      分时仅前 {minuteBatchCap}/{allIntradaySymbols.length} · 受数据源批量上限限制
                      <button
                        type="button"
                        onClick={() => setIntradayCapDismissed(true)}
                        className="text-warning/50 hover:text-warning transition-colors"
                        title="关闭提示"
                      >
                        <X className="h-3 w-3" />
                      </button>
                    </span>
                  )}
                </div>
              </div>

              {/* 筛选面板: 只要原始结果有数据就显示 (哪怕筛完后为空, 用户才能改条件) */}
              {showFilter && (showAll ? allRows.length > 0 : !!result?.rows.length) && (
                <FilterPanel
                  value={filter}
                  onChange={setFilter}
                  onClose={() => setShowFilter(false)}
                  onReset={() => {
                    setFilter(defaultFilter)
                    if (activeStrategy) filterMap.current.delete(activeStrategy)
                  }}
                />
              )}

              {displayRows.length === 0 ? (
                <EmptyState
                  icon={ScanSearch}
                  title={filterActive(filter) ? '筛选后无命中'
                    : !showAll && activeStrategy === EARNINGS_GAP_STRATEGY_ID ? '当日没有已确认的业绩断层跳空' : '这份结果暂无命中'}
                  hint={filterActive(filter)
                    ? '当前筛选条件过严, 试试放宽或重置筛选。'
                    : !showAll && activeStrategy === EARNINGS_GAP_STRATEGY_ID
                      ? '未发现同时满足财报时间、利润条件和完整向上跳空的股票。'
                      : '没有符合当前条件的股票。参数或正式盘后数据更新后，可在 16:00 后点击策略标签检查更新。'}
                />
              ) : (
                <>
                  <ScreenerTable
                    extSeriesData={extSeries.data}
                    extSeriesLoading={extSeries.isLoading}
                    extSeriesError={extSeries.isError}
                    assetType={assetType}
                    rows={displayRows}
                    columns={columns}
                    strategyIdToName={strategyIdToName}
                    symbolStrategyMap={symbolStrategyMap}
                    activeStrategy={activeStrategy}
                    activeSymbol={previewSymbol}
                    watchlistSet={watchlistSet}
                    onPreview={(symbol, name, navList) => { setPreviewSymbol(symbol); setPreviewName(name ?? ''); setPreviewNavList(navList ?? []) }}
                    onAddToWatchlist={(symbol, groupId) => toggleWatchlist.mutate({ symbol, action: 'add', groupId })}
                    onRemoveFromWatchlist={symbol => toggleWatchlist.mutate({ symbol, action: 'remove' })}
                    watchlistPending={toggleWatchlist.isPending}
                    klineData={klineData}
                    dailyKChartVisible={dailyKChartVisible}
                    onToggleDailyKChart={toggleDailyKChart}
                    minuteData={minuteData}
                    intradayChartVisible={intradayChartVisible}
                    onToggleIntradayChart={toggleIntradayChart}
                    intradayAutoRefresh={intradayRefreshEnabled && realtimeRunning}
                    onRefreshIntraday={() => minuteBatch.refetch()}
                    intradayRefreshing={minuteBatch.isFetching}
                    strategyTagsExpanded={strategyTagsExpanded}
                    onToggleStrategyTags={toggleStrategyTags}
                    sort={sort}
                    onSortToggle={toggle}
                  />
                </>
              )}
            </motion.div>
          )}

          {!showAll && !result && !(activeStrategy && pendingRunIds.has(activeStrategy)) && !(activeStrategy === EARNINGS_GAP_STRATEGY_ID && gapControl.busy) && (
            <div className="flex flex-col items-center justify-center py-16 gap-4">
              <div className="w-16 h-16 rounded-2xl bg-accent/5 border border-border flex items-center justify-center">
                <ScanSearch className="h-7 w-7 text-accent/40" />
              </div>
              <div className="flex flex-col items-center gap-1.5">
                <span className="text-sm text-secondary">点击策略卡片查看选股结果</span>
                <span className="text-[11px] text-muted">每天 16:00 后手动检查更新，其他时间复用历史结果</span>
              </div>
            </div>
          )}
        </section>
      </div>

      <ListColumnCustomizer
        columns={columns}
        groups={SCREENER_COLUMN_GROUPS}
        onChange={handleColumnsChange}
        open={customizerOpen}
        onClose={() => setCustomizerOpen(false)}
        title="自定义策略结果列"
        builtinSectionLabel="策略内置列"
        extColumnAlign="center"
      />

      <StockPreviewDialog
        symbol={previewSymbol}
        name={previewName}
        onClose={closePreview}
        navList={previewNavList}
        onNavigate={(sym, n) => { setPreviewSymbol(sym); setPreviewName(n ?? '') }}
      />

      <DefaultStrategyParamsDialog
        show={showDefaultParams}
        onClose={() => setShowDefaultParams(false)}
      />

      <StrategySettingsDialog
        strategyId={settingsStrategyId}
        onClose={() => setSettingsStrategyId(null)}
        onSaved={(limit) => {
          if (settingsStrategyId) {
            setStrategyLimits(prev => ({ ...prev, [settingsStrategyId]: limit }))
            void qc.invalidateQueries({ queryKey: QK.screenerTasksRoot })
            toast('参数已保存，点击策略标签时检查更新', 'success')
          }
        }}
        onDeleted={() => {
          if (settingsStrategyId) {
            removeFromPool(settingsStrategyId)
            const rules = storage.strategyRules.get({})
            delete rules[settingsStrategyId]; storage.strategyRules.set(rules)
            setStrategyLimits(prev => { const next = {...prev}; delete next[settingsStrategyId]; return next })
            qc.invalidateQueries({ queryKey: ['screener-strategies'] })
          }
        }}
      />

      {showPoolDialog && (
        <StrategyPoolDialog
          pool={pool}
          onConfirm={(newPool) => {
            reorderPool(newPool)
          }}
          onClose={() => setShowPoolDialog(false)}
        />
      )}
      <StrategyBuilderDialog
        open={showBuilder}
        onClose={() => setShowBuilder(false)}
        mode={builderMode}
        existingStrategyIds={allStrategyIds}
        onSavedId={async (id, researchOnly) => {
          if (researchOnly) {
            // AI 策略保存为 research_only 草稿, 不进入策略池, 提示用户去策略池发布
            toast('AI 策略已保存为草稿，请在策略池「AI」标签发布后使用', 'success')
            return
          }
          const data = await qc.fetchQuery({ queryKey: QK.screenerStrategies('all'), queryFn: () => api.screenerStrategies(), staleTime: 0 })
          if (!data.presets.some(s => s.id === id)) {
            throw new Error(`策略 ${id} 已保存但未加载，请检查策略代码`)
          }
          addToPool(id)

        }}
      />

      <CompositeStrategyDialog
        open={showComposite}
        onClose={() => setShowComposite(false)}
        onSavedId={async id => {
          await qc.invalidateQueries({ queryKey: ['screener-strategies'] })
          addToPool(id)

        }}
      />

      <StrategyStoreDialog
        open={showStore}
        onClose={() => setShowStore(false)}
      />
    </>
  )
}
