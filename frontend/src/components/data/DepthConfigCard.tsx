import { useState, useEffect } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { usePreferences, useCapabilities } from '@/lib/useSharedQueries'
import { MissingCapChip } from '@/lib/capability-labels'

/**
 * 五档盘口 sealed(真假涨停) 配置内容(纯内容, 无外框, 由父级 Card 包裹)。
 *
 * - 轮询间隔: 常规 10~120s · 数据源具备实时推送能力时 3~120s
 * - 盘后自动管道按能力更新盘口，数据页仍可手动更新
 * - disabled 时(监控关闭)输入框禁用
 */
// 注: 文件名保留 DepthConfigCard.tsx, 导出 DepthConfigContent(纯内容无外框)
export function DepthConfigContent({ disabled }: { disabled?: boolean }) {
  const qc = useQueryClient()
  const prefs = usePreferences()
  const caps = useCapabilities()

  const hasDepth = !!caps.data?.capabilities?.['depth5.batch']
  // 能力判定(非档位): 具备实时推送能力的数据源允许更快的盘口轮询
  const fastPolling = !!caps.data?.capabilities?.['websocket']
  const range = fastPolling ? { lo: 3, hi: 120 } : { lo: 10, hi: 120 }

  const interval = prefs.data?.depth_polling_interval ?? 10

  const [intervalInput, setIntervalInput] = useState(String(Math.round(interval)))

  useEffect(() => { setIntervalInput(String(Math.round(interval))) }, [interval])

  const saveInterval = useMutation({
    mutationFn: (v: number) => api.updateDepthPollingInterval(v),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.preferences }),
  })

  // 无能力: 显示能力缺失说明 + 去数据源配置入口
  if (!hasDepth) {
    return (
      <div className="space-y-2">
        <p className="text-xs text-muted leading-relaxed">
          真假涨停判定依赖五档盘口实时快照,该数据当前不可用。
          配置提供五档盘口的数据源后,连板梯队将自动区分真封板(显示封单量)与假涨停(归入炸板)。
        </p>
        <MissingCapChip capKey="depth5.batch" />
      </div>
    )
  }

  const inputCls = `w-16 h-7 bg-elevated border border-border rounded text-xs text-center px-1 focus:outline-none focus:border-accent/50 ${disabled ? 'opacity-40 cursor-not-allowed' : ''}`

  return (
    <div className="space-y-3">
      {/* 盘中轮询间隔 */}
      <div className="flex items-center justify-between gap-2">
        <div className={disabled ? 'opacity-50' : ''}>
          <div className="text-xs text-secondary">盘中轮询间隔</div>
          <div className="text-[10px] text-muted">范围 {range.lo}~{range.hi} 秒 · 涨跌停过多时系统自动放慢</div>
        </div>
        <div className="flex items-center gap-1">
          <input
            type="number"
            min={range.lo}
            max={range.hi}
            value={intervalInput}
            disabled={disabled}
            onChange={e => setIntervalInput(e.target.value)}
            onBlur={() => {
              if (disabled) return
              let v = Number(intervalInput)
              if (!Number.isFinite(v)) v = range.lo
              v = Math.max(range.lo, Math.min(range.hi, v))
              saveInterval.mutate(v)
            }}
            className={inputCls}
          />
          <span className="text-xs text-muted">秒</span>
        </div>
      </div>

      <div className="space-y-1">
        <div className="text-xs text-secondary">盘后自动更新</div>
        <div className="text-[10px] text-muted leading-relaxed">交易日盘后自动管道会按数据源能力更新盘口；也可在数据页点击“更新五档盘口”手动补采。</div>
      </div>
    </div>
  )
}
