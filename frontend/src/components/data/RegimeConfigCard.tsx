import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Activity, Layers } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'
import { RegimeMaintenancePanel } from './RegimeMaintenancePanel'

/**
 * 市场环境(regime) 计算设置 —— 全量回填分批参数与单项维护。
 *
 * regime 是本地聚合计算(非外部拉取): 首次/regime 表为空时需全量回填多日,
 * 内存与耗时较高。分批参数控制全量回填的内存峰值: 范围超过「每批天数」时
 * 切成多批独立算后拼接, 每批带 warmup 前缀保证滚动窗口指标(ma20)边界正确。
 */
export function RegimeConfigCard() {
  const qc = useQueryClient()
  const prefs = useQuery({ queryKey: QK.preferences, queryFn: api.preferences })

  // 分批参数本地草稿(失焦/回车时整体保存), 避免每次按键都请求
  const batchDays = prefs.data?.regime_batch_days ?? 60
  const warmupDays = prefs.data?.regime_warmup_days ?? 40
  const [draftBatch, setDraftBatch] = useState(String(batchDays))
  const [draftWarmup, setDraftWarmup] = useState(String(warmupDays))

  // prefs 加载后同步一次草稿(仅首次)
  const [synced, setSynced] = useState(false)
  if (!synced && prefs.data) {
    setDraftBatch(String(batchDays))
    setDraftWarmup(String(warmupDays))
    setSynced(true)
  }

  const updateParams = useMutation({
    mutationFn: (params: { batch_days?: number; warmup_days?: number }) =>
      api.updateRegimeBatchParams(params),
    onSuccess: (data) => {
      setDraftBatch(String(data.regime_batch_days))
      setDraftWarmup(String(data.regime_warmup_days))
      qc.invalidateQueries({ queryKey: QK.preferences })
      toast('分批参数已保存', 'success')
    },
    onError: () => toast('保存失败,请检查输入范围', 'error'),
  })

  const saveBatch = () => {
    const n = Math.floor(Number(draftBatch) || 0)
    if (n < 25 || n > 500) {
      toast('每批天数范围 25 ~ 500', 'error')
      setDraftBatch(String(batchDays))
      return
    }
    if (n !== batchDays) updateParams.mutate({ batch_days: n })
  }
  const saveWarmup = () => {
    const n = Math.floor(Number(draftWarmup) || 0)
    if (n < 35 || n > 90) {
      toast('预热天数范围 35 ~ 90', 'error')
      setDraftWarmup(String(warmupDays))
      return
    }
    if (n !== warmupDays) updateParams.mutate({ warmup_days: n })
  }

  return (
    <div className="space-y-3">
      <div className="rounded-card border border-border bg-base/30 px-3 py-2.5">
        <div className="flex items-center gap-1.5 text-xs font-medium text-foreground"><Activity className="h-3.5 w-3.5 text-accent" />随数据更新自动计算</div>
        <p className="mt-1 text-[11px] leading-relaxed text-muted">“更新日常数据”和“补齐历史”都会自动计算市场环境、情绪周期与主线。下方单项重算仅用于维护，无需在每次更新后操作。</p>
      </div>

      {/* 分批参数 */}
      <div className="rounded-card border border-border bg-base/30 px-3 py-2.5">
        <div className="flex items-center gap-1.5">
          <Layers className="h-3.5 w-3.5 text-accent" />
          <span className="text-xs font-medium text-foreground">全量回填分批参数</span>
        </div>
        <div className="mt-0.5 text-[10px] text-muted leading-snug">
          全量重算时按批切片控制内存峰值。每批越小越省内存、批次越多越慢。仅在「全部」重算或首次回填时生效。
        </div>

        <div className="mt-2.5 grid grid-cols-2 gap-2.5">
          <div>
            <label className="text-[10px] text-muted">每批天数(交易日)</label>
            <input
              type="number"
              min={25}
              max={500}
              value={draftBatch}
              onChange={e => setDraftBatch(e.target.value)}
              onBlur={saveBatch}
              onKeyDown={e => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
              disabled={updateParams.isPending}
              className="mt-1 h-7 w-full rounded-input border border-border bg-base px-2 text-xs text-foreground outline-none focus:border-accent disabled:opacity-50"
            />
            <div className="mt-0.5 text-[9px] text-muted">范围 25 ~ 500 · 默认 60</div>
          </div>
          <div>
            <label className="text-[10px] text-muted">预热天数(日历日)</label>
            <input
              type="number"
              min={35}
              max={90}
              value={draftWarmup}
              onChange={e => setDraftWarmup(e.target.value)}
              onBlur={saveWarmup}
              onKeyDown={e => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
              disabled={updateParams.isPending}
              className="mt-1 h-7 w-full rounded-input border border-border bg-base px-2 text-xs text-foreground outline-none focus:border-accent disabled:opacity-50"
            />
            <div className="mt-0.5 text-[9px] text-muted">范围 35 ~ 90 · 默认 40</div>
          </div>
        </div>

        {/* 快捷预设 */}
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] text-muted">快捷:</span>
          {[
            { label: '省内存', batch: 30, warmup: 40 },
            { label: '默认', batch: 60, warmup: 40 },
            { label: '更快', batch: 120, warmup: 40 },
          ].map(p => (
            <button
              key={p.label}
              onClick={() => updateParams.mutate({ batch_days: p.batch, warmup_days: p.warmup })}
              disabled={updateParams.isPending}
              className={`h-5 rounded-btn border px-2 text-[10px] transition-colors disabled:opacity-50 ${
                batchDays === p.batch
                  ? 'border-accent/40 bg-accent/10 text-accent'
                  : 'border-border bg-base text-secondary hover:text-accent hover:border-accent/40'
              }`}
            >
              {p.label} {p.batch}天
            </button>
          ))}
        </div>
      </div>

      {updateParams.isPending && (
        <div className="flex items-center gap-1.5 text-[10px] text-muted">
          <Loader2 className="h-3 w-3 animate-spin" />保存中…
        </div>
      )}
      <div className="pt-4 border-t border-border">
        <RegimeMaintenancePanel />
      </div>
    </div>
  )
}
