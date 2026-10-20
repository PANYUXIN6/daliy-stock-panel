import { useEffect, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Download } from 'lucide-react'
import { api } from '@/lib/api'
import { MissingCapChip, routeCapUsable } from '@/lib/capability-labels'
import { minuteCollectionAffectsQuery, QK } from '@/lib/queryKeys'
import { useCollectorJob } from '@/lib/useCollectorJob'
import { useCapabilityMatrix } from '@/lib/useSharedQueries'

/** 由调用方按股票和日期设置 key；离开图表只停止跟踪，不取消共享采集任务。 */
export function MinuteCollectionButton({ symbol, date }: { symbol: string; date: string }) {
  const qc = useQueryClient()
  const matrix = useCapabilityMatrix()
  const usable = routeCapUsable(matrix.data, 'minute')
  const [jobId, setJobId] = useState<string | null>(null)
  const [reloading, setReloading] = useState(false)
  const mounted = useRef(true)
  const submitting = useRef(false)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  const mutation = useMutation({
    mutationFn: () => api.minuteFetch(symbol, date),
    onSuccess: data => {
      if (mounted.current) setJobId(data.job_id)
    },
    onSettled: () => { submitting.current = false },
  })
  const job = useCollectorJob(jobId, () => {
    setReloading(true)
    void qc.invalidateQueries({
      predicate: query => query.queryKey[0] !== QK.minuteBatch('')[0]
        && minuteCollectionAffectsQuery(query.queryKey, symbol, date),
    }).finally(() => {
      if (mounted.current) setReloading(false)
    })
  }, api.minuteFetchJob)
  const busy = mutation.isPending || job.running || reloading
  const failed = job.data?.status === 'failed'
  const completed = job.data?.status === 'succeeded'
  const taskError = failed ? job.data?.error || '分钟数据采集失败，请重试。' : null
  const retry = mutation.isError || failed || completed
  const start = () => {
    if (usable !== true || busy || submitting.current) return
    submitting.current = true
    setJobId(null)
    mutation.mutate()
  }

  return (
    <div className="flex max-w-full flex-col items-center gap-2 px-4 text-center">
      <button type="button" onClick={start} disabled={usable !== true || busy}
        className="inline-flex items-center gap-1.5 rounded-btn border border-accent/30 bg-accent/10 px-3 py-1.5 text-accent hover:bg-accent/15 disabled:opacity-50">
        <Download className="h-3.5 w-3.5" />
        {mutation.isPending ? '提交中…' : job.running ? '采集中…' : reloading ? '刷新图表中…' : retry ? '重新采集' : '采集该日分钟数据'}
      </button>
      {matrix.isPending && <span className="text-muted">正在检查分钟采集能力…</span>}
      {matrix.isError && usable !== true && <span role="alert" className="text-danger">
        无法读取分钟采集能力，<button type="button" className="underline" onClick={() => void matrix.refetch()}>重新检查</button>
      </span>}
      {!matrix.isPending && !matrix.isError && usable !== true && <div className="flex flex-wrap items-center justify-center gap-2 text-muted">
        <span>请先配置可用的分钟数据源</span><MissingCapChip label="分钟数据" />
      </div>}
      {job.running && <span role="status" className="text-accent">
        {job.error ? '进度加载失败，正在重新读取…' : `${mutation.data?.reused ? '该股票和日期已有采集任务，已加入进度跟踪。' : ''}${job.message}`}
        {job.data ? ` ${Math.round(job.data.progress)}%` : ''}
        {job.error && <button type="button" className="ml-2 underline disabled:opacity-50" disabled={job.isFetching} onClick={() => void job.refetch()}>重试读取进度</button>}
      </span>}
      {mutation.isError && <span role="alert" className="text-danger">{mutation.error.message}</span>}
      {!mutation.isPending && taskError && <span role="alert" className="text-danger">{taskError}</span>}
      {!mutation.isPending && completed && <span role="status" className="text-muted">
        {job.data?.result?.rows === 0 ? '未获取到该日分钟数据，可稍后重新采集。'
          : reloading ? '采集完成，正在读取分钟数据…' : '采集已完成，仍无可展示的分钟数据，可重新采集。'}
      </span>}
    </div>
  )
}
