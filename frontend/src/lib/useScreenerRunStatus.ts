import { useCallback, useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type ScreenerTasksSummary, type ScreenerTaskStatus } from './api'
import { QK } from './queryKeys'

/** 所有标签共用一次只读查询；任务归后端所有，刷新页面后仍可恢复。 */
export function useScreenerRunStatus(asset: 'stock' | 'etf', asOf: string) {
  const qc = useQueryClient()
  const scope = `${asset}|${asOf}`
  const submitting = useRef(new Set<string>())
  const [local, setLocal] = useState<Record<string, { pending?: boolean; error?: string; notice?: string | null }>>({})
  const summary = useQuery({
    queryKey: QK.screenerTasks(asset, asOf),
    queryFn: () => api.screenerTasks(asset, asOf || undefined),
    refetchInterval: query => Object.values(query.state.data?.tasks ?? {}).some(task => !task.done) ? 2000 : false,
  })
  const tasks = summary.data?.tasks ?? {}
  const completion = JSON.stringify(Object.entries(summary.data?.results ?? {}).map(([sid, r]) => [sid, r.computed_at]))
  const published = useRef('')
  useEffect(() => {
    const version = `${scope}|${completion}`
    if (published.current === version) return
    published.current = version
    void qc.invalidateQueries({ queryKey: QK.screenerCachedRoot })
  }, [scope, completion, qc])

  const updateTask = useCallback(async (task: ScreenerTaskStatus, requestedAsset: string) => {
    const queryKey = [QK.screenerTasksRoot[0], requestedAsset]
    await qc.cancelQueries({ queryKey })
    qc.setQueriesData<ScreenerTasksSummary>({ queryKey }, previous => ({
      as_of: null, results: {}, today_ever_counts: {}, updated_at: null,
      ...previous, tasks: { ...previous?.tasks, [task.metadata.strategy_id]: task },
    }))
  }, [qc])

  const submit = useCallback(async (sid: string) => {
    const key = `${scope}|${sid}`
    if (submitting.current.has(key) || (tasks[sid] && !tasks[sid].done)) return
    submitting.current.add(key)
    setLocal(previous => ({ ...previous, [key]: { pending: true } }))
    try {
      const response = await api.screenerSubmitTask(sid, asset, asOf || undefined)
      if (response.task) await updateTask(response.task, asset)
      setLocal(previous => ({ ...previous, [key]: { notice: response.notice } }))
      await qc.invalidateQueries({ queryKey: QK.screenerTasksRoot })
      await qc.invalidateQueries({ queryKey: QK.screenerCachedRoot })
    } catch (error) {
      setLocal(previous => ({ ...previous, [key]: { error: String((error as Error).message ?? error) } }))
      // 提交响应丢失并不等于任务未受理；只读恢复，绝不自动再次提交。
      void qc.invalidateQueries({ queryKey: QK.screenerTasksRoot })
    } finally {
      submitting.current.delete(key)
    }
  }, [scope, asset, asOf, tasks, qc, updateTask])

  const cancel = useCallback(async (sid: string) => {
    const task = tasks[sid]
    if (!task || task.done || task.phase === 'cancelling') return
    try {
      const status = await api.screenerCancelRun(task.run_id)
      await updateTask({ ...task, ...status }, asset)
    } catch (error) {
      setLocal(previous => ({ ...previous, [`${scope}|${sid}`]: { error: String((error as Error).message ?? error) } }))
    }
  }, [tasks, asset, scope, updateTask])

  const pendingIds = [...new Set([
    ...Object.entries(tasks).filter(([, task]) => !task.done).map(([sid]) => sid),
    ...Object.entries(local).filter(([key, state]) => key.startsWith(`${scope}|`) && state.pending).map(([key]) => key.slice(scope.length + 1)),
  ])]
  const errors: Record<string, string> = {}
  const notices: Record<string, string> = {}
  for (const [sid, task] of Object.entries(tasks)) {
    const error = task.error || task.errors[sid]
    if (error) errors[sid] = error
    if (task.phase === 'cancelled') notices[sid] = '已取消，保留上次结果。'
  }
  for (const [key, state] of Object.entries(local)) {
    if (!key.startsWith(`${scope}|`)) continue
    const sid = key.slice(scope.length + 1)
    if (state.error) errors[sid] = state.error
    if (state.notice) notices[sid] = state.notice
  }
  return { summary, tasks, submit, cancel, pendingIds, running: pendingIds.length > 0, errors, notices,
    connectionNotice: summary.isError ? '任务状态暂时获取失败，请重试查询；后台任务不会因此取消。' : null }
}
