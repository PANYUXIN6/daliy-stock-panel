import { useState, useEffect } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Trash2, Download, Calendar } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { MissingCapChip } from '@/lib/capability-labels'
import { useCollectionSyncConfirmation } from '@/lib/useCollectionSyncConfirmation'

// hasCap: 分钟K能力当前是否可用 (路由矩阵判定, 生效源含插件/自定义源)
export function MinuteSyncConfig({ hasCap, onJobStart, disabled = false }: { hasCap: boolean; onJobStart?: (jobId: string) => void; disabled?: boolean }) {
  const qc = useQueryClient()
  const prefs = useQuery({
    queryKey: QK.preferences,
    queryFn: api.preferences,
  })
  const update = useMutation({
    mutationFn: ({ enabled, days, segmentDays }: { enabled: boolean; days: number; segmentDays?: number }) =>
      api.updateMinuteSync(enabled, days, segmentDays),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.preferences }),
  })

  const hasMinuteCap = hasCap
  const enabled = prefs.data?.minute_sync_enabled ?? false
  const days = prefs.data?.minute_sync_days ?? 5
  const segmentDays = prefs.data?.minute_sync_segment_days ?? 20
  const [localDays, setLocalDays] = useState(days)
  const [localSegment, setLocalSegment] = useState(segmentDays)
  const [singleSymbol, setSingleSymbol] = useState('')
  const [singleDays, setSingleDays] = useState('5')
  const singleDaysValue = Number(singleDays)
  const singleDaysValid = Number.isInteger(singleDaysValue) && singleDaysValue >= 1 && singleDaysValue <= 30
  const confirmation = useCollectionSyncConfirmation(`${hasCap}:${prefs.data?.minute_data_provider}:${singleSymbol}:${singleDays}`)
  const single = useMutation({
    mutationFn: ({ symbol, days }: { symbol: string; days: number }) => api.syncMinuteSingle(symbol, days),
    onSuccess: result => {
      void qc.invalidateQueries({ queryKey: QK.pipelineJobs })
      onJobStart?.(result.job_id)
    },
  })

  useEffect(() => { setLocalDays(days) }, [days])
  useEffect(() => { setLocalSegment(segmentDays) }, [segmentDays])

  useEffect(() => { setLocalDays(days) }, [days])
  useEffect(() => { setLocalSegment(segmentDays) }, [segmentDays])

  const handleToggle = () => {
    if (!hasMinuteCap || disabled) return
    update.mutate({ enabled: !enabled, days: localDays })
  }

  const setDays = (v: number) => {
    const clamped = Math.max(1, Math.min(30, v))
    setLocalDays(clamped)
    update.mutate({ enabled, days: clamped })
  }

  // 段大小按交易日数估算自然日区间, 步进 ±5, 范围 [5, 30]。越小越省内存但越慢。
  const setSegment = (v: number) => {
    const clamped = Math.max(5, Math.min(30, v))
    setLocalSegment(clamped)
    update.mutate({ enabled, days: localDays, segmentDays: clamped })
  }

  // 清空分钟K数据 (二次确认)
  const [confirmClear, setConfirmClear] = useState(false)
  const clearMutation = useMutation({
    mutationFn: () => api.clearMinute(),
    onSuccess: () => {
      setConfirmClear(false)
      qc.invalidateQueries({ queryKey: QK.dataStatus })
    },
  })

  // 单独获取 (两个独立按钮, 各自指定天数, 不影响全量同步范围)
  const [fetchingMode, setFetchingMode] = useState<'' | '40d' | '1y'>('')
  const [fetchError, setFetchError] = useState<string | null>(null)
  const handleFetch = (mode: '40d' | '1y') => {
    if (!hasMinuteCap) return
    // 向前扩展按交易日数估算: 小窗口沿用分段大小, 大窗口仍传 365 并分段拉取。
    const fetchDays = mode === '40d' ? localSegment : 365
    setFetchError(null)
    setFetchingMode(mode)
    api.syncMinute(fetchDays, true).then((res) => {
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
      qc.invalidateQueries({ queryKey: QK.dataStatus })
      // 通知主页面跟踪 job 进度 (ActiveJobCard 会显示实时进度+日志)
      if (res.job_id && onJobStart) onJobStart(res.job_id)
    }).catch(error => setFetchError(error.message)).finally(() => setFetchingMode(''))
  }

  return (
    <div className="px-4 pb-4 pt-3 border-t border-accent/20 space-y-3">
      {/* 区块 A: 全量同步范围 */}
      <div className="space-y-2.5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <button
            onClick={handleToggle}
            disabled={!hasMinuteCap || disabled || update.isPending}
            className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors duration-200 shrink-0 ${
              enabled ? 'bg-accent shadow-[0_0_6px_rgba(61,214,140,0.3)]' : 'bg-elevated'
            } ${!hasMinuteCap ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer'}`}
          >
            <span
              className={`inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform duration-200 ${
                enabled ? 'translate-x-[18px]' : 'translate-x-0.5'
              }`}
            />
          </button>
          <span className="text-xs text-foreground font-medium">
            随全量同步{enabled ? '已开启' : '已关闭'}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex items-center">
            <button
              onClick={() => setDays(localDays - 1)}
              disabled={!hasMinuteCap || disabled || update.isPending || localDays <= 1}
              className="h-6 w-6 flex items-center justify-center rounded-l-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
            >−</button>
            <div className="h-6 w-8 flex items-center justify-center border-y border-border text-[11px] font-mono tabular-nums text-foreground bg-base">
              {localDays}
            </div>
            <button
              onClick={() => setDays(localDays + 1)}
              disabled={!hasMinuteCap || disabled || update.isPending || localDays >= 30}
              className="h-6 w-6 flex items-center justify-center rounded-r-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
            >+</button>
          </div>
          <span className="text-[10px] text-muted">自然日</span>
          {!hasMinuteCap && (
            <MissingCapChip capKey="kline.minute.batch" />
          )}
        </div>
      </div>
      <p className="text-[10px] text-muted leading-relaxed">
        批量分钟历史首次回溯 {localDays} 自然日；已有数据从最新时间增量更新到现在。开启随全量同步后，“同步行情与基础数据”也会一并采集。
      </p>

      {/* 分段大小: 按交易日数估算单次 SDK 请求的自然日区间。每段拉完即落盘,避免全量攒内存 OOM。
          两个向前扩展入口共用此设置 (都经过 sync_and_persist_minute)。 */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <span className="text-xs text-foreground font-medium">分段大小</span>
          <span className="text-[10px] text-muted px-1 py-px rounded bg-warning/8 text-warning/80">内存优化</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-muted">约</span>
          <div className="flex items-center">
            <button
              onClick={() => setSegment(localSegment - 5)}
              disabled={!hasMinuteCap || disabled || update.isPending || localSegment <= 5}
              className="h-6 w-6 flex items-center justify-center rounded-l-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
            >−</button>
            <div className="h-6 w-8 flex items-center justify-center border-y border-border text-[11px] font-mono tabular-nums bg-base text-foreground">
              {localSegment}
            </div>
            <button
              onClick={() => setSegment(localSegment + 5)}
              disabled={!hasMinuteCap || disabled || update.isPending || localSegment >= 30}
              className="h-6 w-6 flex items-center justify-center rounded-r-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
            >+</button>
          </div>
          <span className="text-[10px] text-muted">交易日/段</span>
        </div>
      </div>
      <div className="text-[10px] text-muted leading-relaxed -mt-1">
        按交易日数估算自然日区间，节假日会使实际交易日数有偏差。每段拉完即写盘；越小越省内存但越慢，默认 20。
      </div>
      </div>

      {/* 区块 B: 单独获取 (一次性操作, 独立于上方全量同步范围开关) */}
      <div className="pt-3 border-t border-border space-y-2">
        <div className="flex items-center gap-1.5">
          <Download className="h-3 w-3 text-secondary" />
          <span className="text-[11px] text-secondary font-medium">手动获取</span>
          <span className="text-[10px] text-muted">不受全量同步范围开关影响</span>
        </div>
        <div className="grid grid-cols-2 gap-2">
        <button
          onClick={() => handleFetch('40d')}
          disabled={disabled || !hasMinuteCap || fetchingMode !== '' || single.isPending || confirmation.busy}
          className="inline-flex flex-col items-center justify-center gap-0.5 px-2 py-2 rounded-btn bg-accent/90 text-foreground text-xs font-medium hover:bg-accent disabled:opacity-40 transition-colors duration-150"
        >
          {fetchingMode === '40d' ? (
            <><Loader2 className="h-3.5 w-3.5 animate-spin" /><span>获取中…</span></>
          ) : (
            <><Download className="h-3.5 w-3.5" /><span>向前扩展约 {localSegment} 个交易日</span></>
          )}
        </button>
        <button
          onClick={() => handleFetch('1y')}
          disabled={disabled || !hasMinuteCap || fetchingMode !== '' || single.isPending || confirmation.busy}
          className="inline-flex flex-col items-center justify-center gap-0.5 px-2 py-2 rounded-btn border border-amber-400/40 bg-amber-400/10 text-amber-400 text-xs font-medium hover:bg-amber-400/20 disabled:opacity-40 transition-colors duration-150"
        >
          {fetchingMode === '1y' ? (
            <><Loader2 className="h-3.5 w-3.5 animate-spin" /><span>分段获取中…</span></>
          ) : (
            <><Calendar className="h-3.5 w-3.5" /><span>向前扩展约 365 个交易日</span><span className="text-[9px] opacity-70">分段拉取</span></>
          )}
        </button>
        </div>
        {fetchError && <p role="alert" className="text-xs text-danger">{fetchError}</p>}
        <div className="text-[10px] text-muted leading-relaxed">
          A股标的 · 前复权价格 · 从本地最早数据向前估算扩展；无本地数据时从当前回溯。{' '}
          节假日会使实际交易日数有偏差；按上方「分段大小」分段拉取、每段即落盘。
        </div>
      </div>

      <div className="pt-3 border-t border-border space-y-2">
        <div className="text-xs font-medium text-foreground">单标的分钟历史</div>
        <p className="text-[11px] text-muted">按本地交易日历采集指定标的最近 1 ~ 30 个交易日的数据，供分时图读取。</p>
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex-1 min-w-[140px] space-y-1">
            <span className="text-[10px] text-muted">标的代码</span>
            <input value={singleSymbol} onChange={e => setSingleSymbol(e.target.value)} placeholder="600000.SH"
              disabled={single.isPending} className="w-full rounded-input border border-border bg-base px-2 py-1.5 text-xs text-foreground" />
          </label>
          <label className="w-20 space-y-1">
            <span className="text-[10px] text-muted">交易日数</span>
            <input type="number" min={1} max={30} value={singleDays} onChange={e => setSingleDays(e.target.value)}
              disabled={single.isPending} className="w-full rounded-input border border-border bg-base px-2 py-1.5 text-xs text-foreground" />
          </label>
          <button type="button" onClick={() => {
            const input = { symbol: singleSymbol.trim(), days: singleDaysValue }
            confirmation.run({ kind: 'minute', ...input }, () => single.mutate(input))
          }} disabled={disabled || !hasMinuteCap || !singleSymbol.trim() || !singleDaysValid || single.isPending || confirmation.busy || fetchingMode !== ''}
            className="inline-flex items-center gap-1.5 rounded-btn bg-accent/90 px-3 py-1.5 text-xs text-white disabled:opacity-40">
            {single.isPending && <Loader2 className="h-3 w-3 animate-spin" />}获取单标的
          </button>
        </div>
        {single.isError && <p role="alert" className="text-xs text-danger">{single.error.message}</p>}
        {confirmation.error && <p role="alert" className="text-xs text-danger">{confirmation.error}</p>}
      </div>

      {/* 区块 C: 清空 (危险操作, 独立分隔) */}
      <button
        onClick={() => setConfirmClear(true)}
        disabled={disabled || clearMutation.isPending}
        title="清空分钟K数据"
        className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-btn border border-danger/30 text-danger/80 text-xs font-medium hover:bg-danger/10 disabled:opacity-40 transition-colors duration-150"
      >
        <Trash2 className="h-3 w-3" />
        清空分钟K数据
      </button>

      {/* 清空确认弹窗 */}
      {confirmClear && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={() => !clearMutation.isPending && setConfirmClear(false)} />
          <div className="relative rounded-card border border-border bg-surface shadow-2xl mx-4 px-6 py-5 max-w-sm w-full space-y-4">
            <div className="text-sm text-foreground text-center font-medium">确认清空分钟K数据？</div>
            <div className="text-[11px] text-muted text-center leading-relaxed">
              此操作仅删除分钟K (kline_minute) 数据, <span className="text-foreground/80">不影响</span>日K、复权因子、指标等其他数据。清空后可重新获取。
            </div>
            <div className="flex items-center justify-center gap-3">
              <button onClick={() => setConfirmClear(false)} disabled={clearMutation.isPending}
                className="px-4 py-1.5 rounded-btn bg-elevated text-secondary text-xs hover:bg-elevated/80 transition-colors duration-150">取消</button>
              <button onClick={() => clearMutation.mutate()} disabled={clearMutation.isPending}
                className="px-4 py-1.5 rounded-btn bg-danger/90 text-foreground text-xs font-medium hover:bg-danger disabled:opacity-40 transition-colors duration-150">
                {clearMutation.isPending ? <span className="inline-flex items-center gap-1.5"><Loader2 className="h-3 w-3 animate-spin" />清空中…</span> : '确认清空'}
              </button>
            </div>
          </div>
        </div>
      )}
      {confirmation.dialog}
    </div>
  )
}
