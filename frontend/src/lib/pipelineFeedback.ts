import type { PipelineJobSummary } from './api'

export type PipelineOutcome = 'succeeded' | 'failed' | 'cancelled'

/** 兼容未保存 outcome 的旧任务；超时和异常仍是失败，只有用户取消显示取消。 */
export function pipelineOutcome(job: Pick<PipelineJobSummary, 'status' | 'error' | 'outcome'>): PipelineOutcome | null {
  if (job.status === 'succeeded') return 'succeeded'
  if (job.status !== 'failed') return null
  return job.outcome === 'cancelled' || job.error === '用户手动取消' ? 'cancelled' : 'failed'
}
