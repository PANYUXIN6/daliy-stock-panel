import { useState, useCallback, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Check, Gauge, GripVertical, Info, Loader2, RefreshCw, RotateCcw, Timer } from 'lucide-react'
import { DatePicker } from '@/components/DatePicker'
import { api, type AlertEvent } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { DimensionMembersDialog, type DimensionMembersTarget } from '@/components/DimensionMembersDialog'
import { useDataStatus, useCapabilities } from '@/lib/useSharedQueries'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import type { NavItem } from '@/lib/listNav'
import { scoreColor, quoteAge } from '@/components/dashboard/shared'
import { DashboardGrid } from '@/components/dashboard/DashboardGrid'
import { AddWidgetPanel } from '@/components/dashboard/AddWidgetPanel'
import { useDashboardLayout } from '@/components/dashboard/useDashboardLayout'
import { DEFAULT_LAYOUT, widgetDef, type WidgetCtx } from '@/components/dashboard/registry'
import { cloneItems, GRID_COLS, type WidgetType } from '@/components/dashboard/layout'

/** 打开个股预览的来源榜 (用于行高亮与切股导航列表) */
type PreviewSource = 'gain' | 'loss' | 'amount' | 'active' | 'concept' | 'industry' | 'alert'

export function Dashboard() {
  const [selectedDate, setSelectedDate] = useState<string | undefined>()
  const [manualFetching, setManualFetching] = useState(false)
  const [previewStock, setPreviewStock] = useState<{
    symbol: string
    name?: string
    alert?: AlertEvent
    /** 打开来源榜: 仅高亮来源榜的行 */
    source?: PreviewSource
    /** 切股导航列表 (来自来源榜) */
    navList?: NavItem[]
  } | null>(null)
  // 板块成分股弹窗 (概念/行业热度卡片行点击)
  const [dimensionTarget, setDimensionTarget] = useState<DimensionMembersTarget | null>(null)
  // 自定义网格布局(持久化 hook: 后端偏好加载 + 本地改动防抖落盘);
  // 注意必须在早退 return 之前 — Hooks 顺序不可随数据加载状态变化。
  const { items: dashItems, setItems: setDashItems } = useDashboardLayout()
  // 布局编辑态: 入口与控制组(添加组件/恢复默认/完成)在头部「重载」右侧
  const [dashEditing, setDashEditing] = useState(false)
  const placedTypes = useMemo(() => new Set(dashItems.map(it => it.t)), [dashItems])
  const resetDashLayout = useCallback(() => {
    setDashItems(cloneItems(DEFAULT_LAYOUT))
  }, [setDashItems])
  /** 追加组件: 放到当前布局最底部; ext-link 用唯一 id 支持多实例 */
  const addDashWidget = useCallback((t: WidgetType, props?: Record<string, string>) => {
    const def = widgetDef(t)
    if (!def) return
    // 内置组件单实例: 已存在则忽略
    if (t !== 'ext-link' && dashItems.some(it => it.t === t)) return
    const maxY = dashItems.reduce((m, it) => Math.max(m, it.y + it.h), 0)
    const id = t === 'ext-link' ? `ext-link-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}` : t
    setDashItems([...dashItems, { i: id, t, x: 0, y: maxY, w: Math.min(def.defW, GRID_COLS), h: def.defH, p: props }])
  }, [dashItems, setDashItems])
  const dataStatus = useDataStatus({ staleTime: 60_000 })
  const overview = useQuery({
    queryKey: QK.overviewMarket(selectedDate),
    queryFn: () => api.overviewMarket(selectedDate),
    staleTime: 5_000,
    placeholderData: (prev) => prev,
  })
  const data = overview.data
  const caps = useCapabilities()
  const hasDepth = !!caps.data?.capabilities?.['depth5.batch']
  const sealedReady = !!data?.limit?.sealed_ready
  const isSealedDegrade = !hasDepth || !sealedReady

  const handleRefresh = () => {
    setManualFetching(true)
    void overview.refetch().finally(() => setManualFetching(false))
  }

  if (overview.isLoading && !data) {
    return (
      <div className="flex h-full items-center justify-center bg-base">
        <div className="flex items-center gap-2 text-sm text-muted">
          <Loader2 className="h-4 w-4 animate-spin" /> 加载市场看板…
        </div>
      </div>
    )
  }

  if (!data) {
    return (
      <div className="flex h-full items-center justify-center bg-base p-6">
        <div className="rounded-card border border-border bg-surface p-6 text-center">
          <div className="text-sm text-danger">看板加载失败</div>
          <button onClick={() => overview.refetch()} className="mt-3 rounded-btn bg-accent px-3 py-1.5 text-xs font-medium text-white">重试</button>
        </div>
      </div>
    )
  }

  const score = data.emotion?.score ?? 50
  const latestDate = dataStatus.data?.enriched?.latest_date ?? null
  const currentDate = selectedDate ?? data.as_of ?? ''
  const quoteRunning = (!selectedDate || selectedDate === latestDate) && data.quote_status?.running
  // 实时模式: none / watchlist / full_market。
  // watchlist 模式仅自选 ≤5 只实时, 看板呈现的大盘数据实为盘后快照, 需提示避免误读。
  const quoteMode = data.quote_status?.mode as ('none' | 'watchlist' | 'full_market') | undefined

  // 网格组件渲染上下文: 数据切片 + 交互回调统一由页面层供给
  const widgetCtx: WidgetCtx = {
    data,
    score,
    hasDepth,
    sealedReady,
    isSealedDegrade,
    activeSymbol: source => (previewStock?.source === source ? previewStock.symbol : undefined),
    openStock: (source, symbol, name, navList) =>
      setPreviewStock({ symbol, name, navList, source: source as PreviewSource }),
    openDimension: setDimensionTarget,
    openAlert: (event, navList) => {
      if (event.symbol) setPreviewStock({ symbol: event.symbol, name: event.name ?? undefined, alert: event, source: 'alert', navList })
    },
  }

  return (
    <div className="min-h-full bg-base p-1.5">
      <div className="relative mb-1.5 flex flex-wrap items-center justify-between gap-2 overflow-hidden rounded-card border border-border bg-gradient-to-r from-surface/90 to-surface/70 px-3 py-1.5 shadow-[0_1px_3px_hsl(var(--border)/0.4)] backdrop-blur-sm">
        <div className="pointer-events-none absolute left-0 top-0 h-full w-1 bg-gradient-to-b from-accent to-accent/20" aria-hidden />
        <div className="flex items-center gap-2">
          <Gauge className="h-4 w-4 text-accent" />
          <h1 className="text-[16px] leading-6 font-semibold text-foreground">市场看板</h1>
          <span
            className="rounded-full border px-2 py-0.5 text-[10px] font-medium"
            style={{
              color: scoreColor(score),
              borderColor: `${scoreColor(score)}40`,
              background: `${scoreColor(score)}14`,
            }}
          >
            {data.emotion.label} · {score}
          </span>
        </div>
        <div className="flex items-center gap-3 text-[11px] text-muted">
          {currentDate ? (
            <DatePicker
              value={currentDate}
              onChange={setSelectedDate}
              min={dataStatus.data?.enriched?.earliest_date ?? undefined}
              max={latestDate ?? undefined}
              className="w-32"
            />
          ) : (
            <span className="font-mono text-secondary">—</span>
          )}
          <span className="flex items-center gap-1"><Timer className="h-3 w-3" />{quoteAge(data.quote_status?.quote_age_ms)}</span>
          <span className={quoteRunning ? 'text-accent' : 'text-warning'}>{quoteRunning ? '实时' : '非实时'}</span>
          <button
            onClick={handleRefresh}
            disabled={manualFetching}
            className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground disabled:opacity-50"
          >
            <RefreshCw className={`h-3 w-3 ${manualFetching ? 'animate-spin' : ''}`} />重载
          </button>
          {!dashEditing ? (
            <button
              onClick={() => setDashEditing(true)}
              title="拖拽调整组件位置与宽高"
              className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground hover:border-accent/40"
            >
              <GripVertical className="h-3 w-3" />自定义布局
            </button>
          ) : (
            <>
              <AddWidgetPanel placedTypes={placedTypes} onAdd={addDashWidget} />
              <button
                onClick={resetDashLayout}
                className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground"
              >
                <RotateCcw className="h-3 w-3" />恢复默认
              </button>
              <button
                onClick={() => setDashEditing(false)}
                className="inline-flex items-center gap-1 rounded-btn bg-accent px-2.5 py-1 text-[11px] font-medium text-white transition-colors hover:bg-accent/90"
              >
                <Check className="h-3 w-3" />完成
              </button>
            </>
          )}
        </div>
      </div>

      {/* 自选实时模式提示: 大盘看板为盘后数据, 仅自选股实时。避免用户误读为全市场实时。 */}
      {quoteMode === 'watchlist' && (
        <div className="mb-1.5 flex items-start gap-2 rounded-card border border-amber-500/30 bg-amber-500/8 px-3 py-1.5 text-[11px] leading-relaxed">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
          <div className="min-w-0 flex-1 text-secondary">
            当前为「自选实时」模式,看板展示的大盘数据为<strong className="text-foreground">盘后快照</strong>(最新有数据日),并非盘中实时;
            仅自选股({data.quote_status?.watchlist_symbol_count ?? 0} 只)支持实时监控。
            <span className="ml-1 text-accent">全市场实时依赖数据源支持</span>
          </div>
        </div>
      )}

      <DashboardGrid ctx={widgetCtx} items={dashItems} onItemsChange={setDashItems} editing={dashEditing} />

      <StockPreviewDialog
        symbol={previewStock?.symbol ?? null}
        name={previewStock?.name}
        triggerInfo={previewStock?.alert ? {
          price: previewStock.alert.price ?? null,
          changePct: previewStock.alert.change_pct ?? null,
          ts: previewStock.alert.ts,
          signals: previewStock.alert.signals,
          message: previewStock.alert.message,
        } : null}
        navList={previewStock?.navList}
        onNavigate={(sym, n) => setPreviewStock(prev => prev ? { ...prev, symbol: sym, name: n, alert: undefined } : prev)}
        onClose={() => setPreviewStock(null)}
      />
      <DimensionMembersDialog
        target={dimensionTarget}
        onClose={() => setDimensionTarget(null)}
        onStockClick={(symbol, name) => {
          setDimensionTarget(null)
          setPreviewStock({ symbol, name })
        }}
      />
    </div>
  )
}
