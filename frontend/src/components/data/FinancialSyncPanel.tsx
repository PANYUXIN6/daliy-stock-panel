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
          <p className="mt-1 text-[11px] text-muted">仅更新利润表，保留历史版本，供业绩断层及利润相关因子使用。</p>
        </div>
        <button type="button" onClick={() => start('income')} disabled={!hasCap || busy}
          className="shrink-0 inline-flex items-center gap-1 px-3 py-1.5 rounded-btn bg-accent/90 text-white text-xs disabled:opacity-40">
          {busy && <Loader2 className="h-3 w-3 animate-spin" />}更新利润表
        </button>
      </div>
      {!hasCap && <MissingCapChip capKey="financial" />}
      <div className="flex items-center justify-between gap-3 rounded-btn border border-border px-3 py-2 text-xs">
        <span className="text-foreground">利润表</span>
        <span className="text-muted">{(status.data?.tables.income?.rows ?? 0).toLocaleString()} 条</span>
      </div>
      <button type="button" onClick={() => start('unfinished')} disabled={!hasCap || busy}
        title="从未完成的利润表断点继续"
        className="text-[11px] text-accent hover:text-foreground disabled:opacity-40">继续未完成</button>
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
