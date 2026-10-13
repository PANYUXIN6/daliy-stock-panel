import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, X } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'

export function RegimeMaintenancePanel() {
  const qc = useQueryClient()
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [savingFilter, setSavingFilter] = useState(false)
  const mainline = useQuery({
    queryKey: QK.regimeMainline('concept'),
    queryFn: () => api.regimeMainline(),
  })
  const invalidate = async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ['regime-history'] }),
      qc.invalidateQueries({ queryKey: ['regime-states'] }),
      qc.invalidateQueries({ queryKey: QK.regimeLatest }),
      qc.invalidateQueries({ queryKey: ['regime-phases'] }),
      qc.invalidateQueries({ queryKey: ['regime-mainline'] }),
      qc.invalidateQueries({ queryKey: QK.regimeCoverage }),
    ])
  }
  const recompute = useMutation({
    mutationFn: (range: { start?: string; end?: string }) => api.regimeRecompute(range.start, range.end),
    onSuccess: async result => {
      toast(`环境重算完成 · ${result.computed} 天`, 'success')
      await invalidate()
    },
  })
  const recomputeMainline = useMutation({
    mutationFn: api.regimeMainlineRecompute,
    onSuccess: async result => {
      toast(`主线重算完成 · ${result.rows} 条`, 'success')
      await invalidate()
    },
  })
  const busy = recompute.isPending || recomputeMainline.isPending || savingFilter
  const validRange = !!start && !!end && start <= end
  const startRecompute = (range: { start?: string; end?: string }) => {
    recomputeMainline.reset()
    recompute.mutate(range)
  }
  const startMainlineRecompute = () => {
    recompute.reset()
    recomputeMainline.mutate()
  }

  return (
    <div className="space-y-3">
      <h4 className="text-xs font-medium text-foreground">历史环境与主线维护</h4>
      <p className="text-[11px] text-muted">使用本地日线和指标重算共享历史，完成后业务页面读取更新结果。</p>
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex-1 min-w-[130px] space-y-1">
          <span className="text-[10px] text-muted">开始日期</span>
          <input type="date" value={start} onChange={e => setStart(e.target.value)} disabled={busy}
            className="w-full rounded-input border border-border bg-base px-2 py-1.5 text-xs text-foreground" />
        </label>
        <label className="flex-1 min-w-[130px] space-y-1">
          <span className="text-[10px] text-muted">结束日期</span>
          <input type="date" value={end} onChange={e => setEnd(e.target.value)} disabled={busy}
            className="w-full rounded-input border border-border bg-base px-2 py-1.5 text-xs text-foreground" />
        </label>
        <button type="button" onClick={() => startRecompute({ start, end })} disabled={busy || !validRange}
          className="rounded-btn bg-accent/90 px-3 py-1.5 text-xs text-white disabled:opacity-40">重算区间</button>
      </div>
      <div className="flex flex-wrap gap-2">
        <button type="button" onClick={() => startRecompute({})} disabled={busy}
          className="rounded-btn border border-border px-3 py-1.5 text-xs text-secondary hover:text-accent disabled:opacity-40">全量重算环境</button>
        <button type="button" onClick={startMainlineRecompute} disabled={busy}
          className="rounded-btn border border-border px-3 py-1.5 text-xs text-secondary hover:text-accent disabled:opacity-40">仅重算主线</button>
      </div>
      {busy && <p role="status" className="inline-flex items-center gap-1 text-xs text-accent"><Loader2 className="h-3 w-3 animate-spin" />重算中，请等待…</p>}
      {(recompute.error || recomputeMainline.error) && <p role="alert" className="text-xs text-danger">{(recompute.error ?? recomputeMainline.error)?.message}</p>}
      <div className="pt-3 border-t border-border">
        <h4 className="text-xs font-medium text-foreground">主线全局过滤</h4>
        {mainline.isLoading && <p className="mt-2 text-xs text-muted">读取过滤配置…</p>}
        {mainline.isError && <p role="alert" className="mt-2 text-xs text-danger">{mainline.error.message}</p>}
        {mainline.data && <MainlineFilterPanel filter={mainline.data.filter} onDone={invalidate} disabled={busy} onSavingChange={setSavingFilter} />}
      </div>
    </div>
  )
}

function MainlineFilterPanel({ filter, onDone, disabled, onSavingChange }: {
  filter: { min_members: number; max_members: number; blacklist: string[]; exclude_st?: boolean } | undefined
  onDone: () => Promise<void>
  disabled: boolean
  onSavingChange: (saving: boolean) => void
}) {
  const [minMembers, setMinMembers] = useState(String(filter?.min_members ?? 4))
  const [maxMembers, setMaxMembers] = useState(String(filter?.max_members ?? 600))
  const [blacklist, setBlacklist] = useState<string[]>(filter?.blacklist ?? [])
  const [excludeSt, setExcludeSt] = useState(filter?.exclude_st ?? true)
  const [input, setInput] = useState('')
  const [saving, setSaving] = useState(false)

  // 面板可能先于过滤配置查询挂载: 首次拿到真实 filter 时回填一次, 避免用户在
  // 默认值上点「保存并重算」用 4/600 覆盖真实过滤配置; 只同步首轮, 不打断编辑
  const seededRef = useRef(false)
  useEffect(() => {
    if (!filter || seededRef.current) return
    seededRef.current = true
    setMinMembers(String(filter.min_members))
    setMaxMembers(String(filter.max_members))
    setBlacklist(filter.blacklist)
    setExcludeSt(filter.exclude_st ?? true)
  }, [filter])

  const addTag = () => {
    const v = input.trim()
    if (v && !blacklist.includes(v)) setBlacklist([...blacklist, v])
    setInput('')
  }

  const save = async () => {
    setSaving(true)
    onSavingChange(true)
    try {
      await api.mainlineFilterUpdate({
        min_members: Math.max(1, Number(minMembers) || 4),
        max_members: Math.max(50, Number(maxMembers) || 600),
        blacklist,
        exclude_st: excludeSt,
      })
      const stChanged = excludeSt !== (filter?.exclude_st ?? true)
      if (stChanged) {
        // 口径切换影响情绪周期驱动指标, 需全量重算 regime+主线(较重, 需等待)
        await api.regimeRecompute()
        toast('过滤已保存, 主线与情绪周期已全量重算', 'success')
      } else {
        await api.regimeMainlineRecompute()
        toast('过滤已保存, 主线已重算', 'success')
      }
      await onDone()
    } catch (e) {
      toast(`保存失败 · ${String((e as Error)?.message || e)}`, 'error')
    } finally {
      setSaving(false)
      onSavingChange(false)
    }
  }

  return (
    <div className="mt-2 rounded-btn border border-border bg-base/40 p-2.5">
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1">
          <span className="text-[10px] text-muted">成员数上限(过滤宽基标签)</span>
          <input type="number" min={50} max={5000} value={maxMembers}
            onChange={e => setMaxMembers(e.target.value)}
            className="h-7 w-24 rounded-input border border-border bg-base px-2 text-xs text-foreground outline-none focus:border-accent" />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[10px] text-muted">成员数下限</span>
          <input type="number" min={1} max={200} value={minMembers}
            onChange={e => setMinMembers(e.target.value)}
            className="h-7 w-20 rounded-input border border-border bg-base px-2 text-xs text-foreground outline-none focus:border-accent" />
        </label>
        <div className="flex min-w-[220px] flex-1 flex-col gap-1">
          <span className="text-[10px] text-muted">按名称屏蔽(回车添加)</span>
          <div className="flex items-center gap-1.5">
            <input value={input} onChange={e => setInput(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addTag() } }}
              placeholder="如: 融资融券、沪股通"
              className="h-7 flex-1 rounded-input border border-border bg-base px-2 text-xs text-foreground outline-none focus:border-accent" />
          </div>
          {blacklist.length > 0 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {blacklist.map(b => (
                <span key={b} className="inline-flex items-center gap-1 rounded bg-accent/10 px-1.5 py-px text-[10px] text-accent">
                  {b}
                  <button onClick={() => setBlacklist(blacklist.filter(x => x !== b))} className="hover:text-foreground">
                    <X className="h-2.5 w-2.5" />
                  </button>
                </span>
              ))}
            </div>
          )}
        </div>
        <button onClick={save} disabled={saving || disabled}
          className="h-7 rounded-btn bg-accent px-3 text-xs font-medium text-white hover:bg-accent/90 disabled:opacity-50">
          {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : '保存并重算'}
        </button>
      </div>
      <div className="mt-2 flex items-center gap-2 border-t border-border/60 pt-2">
        <button
          type="button"
          role="switch"
          aria-checked={excludeSt}
          aria-label="统计剔除 ST 股"
          onClick={() => setExcludeSt(v => !v)}
          className={cn(
            'relative inline-flex h-4.5 w-8 items-center rounded-full border transition-all duration-200',
            excludeSt ? 'border-accent/50 bg-accent' : 'border-border bg-elevated hover:border-muted',
          )}
        >
          <span className={cn(
            'inline-block h-3 w-3 rounded-full border border-black/5 bg-white shadow-sm transition-transform duration-200',
            excludeSt ? 'translate-x-[17px]' : 'translate-x-0.5',
          )} />
        </button>
        <span className="text-[11px] text-secondary">
          统计剔除 ST 股
          <span className="ml-1.5 text-[9px] text-muted">主线 + 情绪周期统一口径; 切换后自动全量重算(约 1-2 分钟)</span>
        </span>
      </div>
      <div className="mt-1.5 text-[9px] text-muted">
        说明: 成分股数超过上限的概念(如 融资融券~7700家/沪深股通~3300家)视为宽基/风格标签, 不参与主线排名;
        风险警示股(名称含 ST)不参与涨停梯队统计 — 主板 5% 便宜板时代曾系统性霸榜, 且 ST 是状态桶而非题材。修改后自动重算全部历史(秒级~分钟级)。
      </div>
    </div>
  )
}
