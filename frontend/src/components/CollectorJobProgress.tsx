import type { useCollectorJob } from '@/lib/useCollectorJob'

export function CollectorJobProgress({ job }: { job: ReturnType<typeof useCollectorJob> }) {
  if (!job.running) return null
  return (
    <span className="inline-flex max-w-full items-center gap-2 text-xs text-accent" role="status">
      <span className="truncate">{job.error ? '进度加载失败，正在重新读取' : job.message} {job.data ? `${Math.round(job.data.progress)}%` : ''}</span>
      <button type="button" className="shrink-0 text-secondary hover:text-foreground disabled:opacity-50"
        disabled={job.cancel.isPending} onClick={() => job.cancel.mutate()}>
        {job.cancel.isPending ? '取消中…' : '取消任务'}
      </button>
      {job.cancel.isError && <span role="alert" className="text-danger">{job.cancel.error?.message || '取消失败，请重试'}</span>}
    </span>
  )
}
