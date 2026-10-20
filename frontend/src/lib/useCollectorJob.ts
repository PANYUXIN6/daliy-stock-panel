import { useEffect, useRef } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './api'
import { QK } from './queryKeys'

/** 采集任务由后台持有；页面只查询进度并发送协作式取消。 */
export function useCollectorJob(jobId: string | null, onFinished?: () => void, getJob = api.pipelineJob) {
  const qc = useQueryClient()
  const onFinishedRef = useRef(onFinished)
  onFinishedRef.current = onFinished
  const finishedId = useRef<string | null>(null)
  const query = useQuery({
    queryKey: QK.pipelineJob(jobId ?? ''),
    queryFn: () => getJob(jobId!),
    enabled: !!jobId,
    refetchInterval: query => {
      const status = query.state.data?.status
      return status === 'succeeded' || status === 'failed' ? false : 1_000
    },
  })
  const terminal = query.data?.status === 'succeeded' || query.data?.status === 'failed'
  useEffect(() => {
    if (!jobId || !terminal || finishedId.current === jobId) return
    finishedId.current = jobId
    void qc.invalidateQueries({ queryKey: QK.pipelineJobs })
    onFinishedRef.current?.()
  }, [jobId, terminal, qc])
  const cancel = useMutation({
    mutationFn: (id: string) => api.pipelineJobCancel(id),
    onSuccess: (_result, id) => qc.invalidateQueries({ queryKey: QK.pipelineJob(id) }),
  })
  return {
    ...query,
    running: !!jobId && !terminal,
    cancel: {
      ...cancel,
      mutate: () => { if (jobId) cancel.mutate(jobId) },
      isPending: cancel.isPending && cancel.variables === jobId,
      isError: cancel.isError && cancel.variables === jobId,
    },
    message: query.data?.status === 'pending' ? '等待采集…'
      : query.data?.log.at(-1)?.msg ?? '正在采集…',
  }
}
