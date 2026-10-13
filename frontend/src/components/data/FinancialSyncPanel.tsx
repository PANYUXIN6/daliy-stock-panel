import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { useFinancialStatus, useFinancialExecutionStatus, useFinancialSync, FINANCIAL_QK } from '@/lib/useFinancials'
import { useCollectorJob } from '@/lib/useCollectorJob'
import { CollectorJobProgress } from '@/components/CollectorJobProgress'
import { MissingCapChip } from '@/lib/capability-labels'
import { QK } from '@/lib/queryKeys'
import { api } from '@/lib/api'
import { useCollectionSyncConfirmation } from '@/lib/useCollectionSyncConfirmation'

const TABLES = [
  ['metrics', '核心指标'],
  ['income', '利润表'],
  ['balance_sheet', '资产负债表'],
  ['cash_flow', '现金流量表'],
  ['shares', '股本表'],
] as const

export function FinancialSyncPanel({ hasCap, onJobStart, disabled = false }: {
  hasCap: boolean
  onJobStart: (jobId: string) => void
  disabled?: boolean
}) {
  const qc = useQueryClient()
  const prefs = useQuery({ queryKey: QK.preferences, queryFn: api.preferences })
  const status = useFinancialStatus()
  const execution = useFinancialExecutionStatus()
  const sync = useFinancialSync()
  const confirmation = useCollectionSyncConfirmation(`${hasCap}:${prefs.data?.financial_data_provider}`)
  const [submittedJobId, setSubmittedJobId] = useState<string | null>(null)
  const [showTableMaintenance, setShowTableMaintenance] = useState(false)
  const job = useCollectorJob(execution.data?.active_job_id ?? submittedJobId, () => {
    void qc.invalidateQueries({ queryKey: FINANCIAL_QK.executionStatus })
    void qc.invalidateQueries({ queryKey: ['financials'] })
    void qc.invalidateQueries({ queryKey: QK.dataStatus })
  })
  const busy = disabled || confirmation.busy || sync.isPending || execution.isLoading || execution.isError || !!execution.data?.syncing || job.running
  const start = (table: string) => confirmation.run({ kind: 'financial', table }, () => sync.mutate(table, {
    onSuccess: result => {
      setSubmittedJobId(result.job_id)
      onJobStart(result.job_id)
    },
  }))

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h4 className="text-xs font-medium text-foreground">财务数据同步</h4>
          <p className="mt-1 text-[11px] text-muted">从财务数据源采集并保存到本地，供财务分析和 AI 分析使用。</p>
        </div>
        <button type="button" onClick={() => start('all')} disabled={!hasCap || busy}
          className="shrink-0 inline-flex items-center gap-1 px-3 py-1.5 rounded-btn bg-accent/90 text-white text-xs disabled:opacity-40">
          {busy && <Loader2 className="h-3 w-3 animate-spin" />}全部同步
        </button>
      </div>
      {!hasCap && <MissingCapChip capKey="financial" />}
      <div className="space-y-2">
        {TABLES.map(([table, label]) => (
          <div key={table} className="flex items-center justify-between gap-3 rounded-btn border border-border px-3 py-2 text-xs">
            <div>
              <span className="text-foreground">{label}</span>
              <span className="ml-2 text-muted">{(status.data?.tables[table]?.rows ?? 0).toLocaleString()} 条</span>
            </div>
            {showTableMaintenance && <button type="button" onClick={() => start(table)} disabled={!hasCap || busy}
              className="text-accent hover:text-foreground disabled:opacity-40" aria-label={`同步${label}`}>更新</button>}
          </div>
        ))}
      </div>
      <button type="button" aria-expanded={showTableMaintenance} onClick={() => setShowTableMaintenance(value => !value)}
        className="text-[11px] text-secondary hover:text-accent">{showTableMaintenance ? '收起单表维护' : '单表维护'}</button>
      <CollectorJobProgress job={job} />
      {confirmation.error && <p role="alert" className="text-xs text-danger">{confirmation.error}</p>}
      {sync.isError && <p role="alert" className="text-xs text-danger">{sync.error.message}</p>}
      {job.data?.status === 'failed' && <p role="alert" className="text-xs text-danger">{job.data.error ?? '财务同步失败'}</p>}
      {job.data?.status === 'succeeded' && <p role="status" className="text-xs text-accent">财务同步完成</p>}
      {status.isError && <p role="alert" className="text-xs text-danger">{status.error.message}</p>}
      {execution.isError && <p role="alert" className="text-xs text-warning">采集执行状态不可用：{execution.error.message}。本地财务数据仍可浏览。</p>}
      {confirmation.dialog}
    </div>
  )
}
