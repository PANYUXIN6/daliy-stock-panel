import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { MissingCapChip } from '@/lib/capability-labels'
import { DatePicker } from '@/components/DatePicker'

function pad(n: number) { return String(n).padStart(2, '0') }
function todayStr() {
  const d = new Date()
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}
/** 往前推 N 天 */
function daysAgo(n: number): string {
  const d = new Date()
  d.setDate(d.getDate() - n)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

// hasCap: 日K批量能力当前是否可用 (路由矩阵判定, 生效源含插件/自定义源)
export function RepairDailyPanel({ hasCap, hasAdjCap, isRunning, latestDate, onStart }: {
  hasCap: boolean
  hasAdjCap: boolean
  isRunning: boolean
  latestDate: string | null
  onStart: (jobId: string) => void
}) {
  const qc = useQueryClient()
  const hasBatchCap = hasCap

  // 默认起始日期: 当前日期往前推 30 天 (兼顾补缺口 + 复核近期数据, 成本不高)
  const [startDate, setStartDate] = useState(daysAgo(30))

  const repair = useMutation({
    mutationFn: () => api.repairDaily(startDate),
    onSuccess: ({ job_id }) => {
      onStart(job_id)
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
    },
  })

  const today = todayStr()
  const canSubmit = hasBatchCap && hasAdjCap && !isRunning && !repair.isPending && startDate <= today
  const gapDays = latestDate
    ? Math.max(0, Math.round((Date.parse(today) - Date.parse(latestDate)) / 86400000))
    : null

  return (
    <div className="space-y-3">
      <div className="rounded-card bg-accent/8 border border-accent/20 p-3 space-y-1.5">
        <div className="text-xs text-foreground">
          从选定日期补齐股票与核心指数历史，并自动更新技术指标、市场环境、情绪周期与主线。
        </div>
        <div className="text-[10px] text-muted leading-relaxed">
          同时更新股票名单和板块分类。已有数据按股票和日期合并，无需先清除；分钟历史单独更新。
        </div>
      </div>

      {/* 起始日期 — 用户主要操作,放上面 */}
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-secondary">起始日期</span>
        <DatePicker
          value={startDate}
          onChange={setStartDate}
          max={today}
          align="right"
          buttonClassName="font-mono text-xs"
        />
      </div>

      {/* 本地最新数据 — 参考信息,放下面 */}
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-muted">本地最新数据</span>
        <span className="font-mono text-xs text-secondary">
          {latestDate ?? '—'}
          {gapDays !== null && gapDays > 0 && (
            <span className="ml-2 text-warning/90">已落后 {gapDays} 天</span>
          )}
        </span>
      </div>

      <div className="text-[10px] text-muted -mt-1">
        将重拉 <span className="font-mono text-secondary">{startDate}</span>
        {' → '}<span className="font-mono text-secondary">{today}</span> 的行情，并完成关联分析计算。历史越长，耗时越久。
      </div>

      <button
        onClick={() => repair.mutate()}
        disabled={!canSubmit}
        className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-btn bg-accent/90 text-white text-xs font-medium hover:bg-accent disabled:opacity-40 disabled:pointer-events-none transition-colors duration-150"
      >
        {repair.isPending ? (
          <>
            <Loader2 className="h-3 w-3 animate-spin" />
            请求中…
          </>
        ) : (
          <>开始补齐</>
        )}
      </button>

      {repair.error && <p role="alert" className="text-xs text-danger">{repair.error.message}</p>}

      {!hasBatchCap && (
        <span className="block text-center">
          <MissingCapChip capKey="kline.daily.batch" />
        </span>
      )}
      {!hasAdjCap && <span className="block text-center"><MissingCapChip capKey="adj_factor" /></span>}
    </div>
  )
}
