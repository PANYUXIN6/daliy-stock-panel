import { useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Star } from 'lucide-react'
import { ApiError, api, type EveningFeedback, type EveningFeedbackRequest, type EveningPersonalRecord } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

type SaveState = { pending: boolean; error?: string; retry?: EveningFeedbackRequest }

/** 保存明确状态而非服务端翻转；未知网络结果重试使用同一请求标识。 */
export function useEveningFeedback(userId: string) {
  const client = useQueryClient()
  const inFlight = useRef(new Set<string>())
  const [states, setStates] = useState<Record<string, SaveState>>({})
  const submit = async (episodeId: string, request: EveningFeedbackRequest) => {
    if (inFlight.current.has(episodeId)) return
    inFlight.current.add(episodeId)
    setStates(previous => ({ ...previous, [episodeId]: { pending: true } }))
    try {
      const feedback = await api.eveningFeedback(request)
      client.setQueriesData<{ records?: EveningPersonalRecord[] }>({ queryKey: QK.eveningPersonalRoot(userId) }, previous => previous?.records ? {
        ...previous, records: previous.records.map(record => record.episode_id === episodeId ? { ...record, feedback } : record),
      } : previous)
      setStates(previous => ({ ...previous, [episodeId]: { pending: false } }))
      void client.invalidateQueries({ queryKey: QK.eveningPersonalRoot(userId) })
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409
      if (conflict) await client.invalidateQueries({ queryKey: QK.eveningPersonalRoot(userId) })
      setStates(previous => ({ ...previous, [episodeId]: { pending: false,
        error: conflict ? '记录已在另一处更新，已重新读取，请确认后再操作。' : (error instanceof Error ? error.message : '保存失败，请重试'),
        retry: conflict ? undefined : request,
      } }))
    } finally { inFlight.current.delete(episodeId) }
  }
  const change = (record: EveningPersonalRecord, feedback: Pick<EveningFeedback, 'starred' | 'outcome'>) => {
    // getRandomValues 同时适用于局域网 HTTP，不能依赖仅安全上下文提供的 randomUUID。
    const requestId = [...crypto.getRandomValues(new Uint8Array(16))].map(value => value.toString(16).padStart(2, '0')).join('')
    void submit(record.episode_id, { date: record.day, version: record.version, symbol: record.symbol, ...feedback,
      expected_revision: record.feedback.revision, request_id: requestId })
  }
  return { states, change, retry: (episodeId: string) => { const request = states[episodeId]?.retry; if (request) void submit(episodeId, request) } }
}

export function EveningFeedbackButtons({ name, record, state, disabled, onChange, onRetry }: {
  name: string
  record?: EveningPersonalRecord
  state?: SaveState
  disabled?: boolean
  onChange: (record: EveningPersonalRecord, feedback: Pick<EveningFeedback, 'starred' | 'outcome'>) => void
  onRetry: () => void
}) {
  const feedback = record?.feedback
  const blocked = disabled || !record || state?.pending
  const base = 'flex h-10 w-10 shrink-0 items-center justify-center rounded-md border text-sm font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:cursor-not-allowed disabled:opacity-40'
  const label = (text: string, selected: boolean) => `${name}：${text}${selected ? '，再次点击可取消' : ''}`
  return <div className="space-y-1.5" onClick={event => event.stopPropagation()}>
    <div className="flex items-center gap-1.5" role="group" aria-label={`${name}的个人反馈`} aria-busy={state?.pending || undefined}>
      <button type="button" aria-label={label('符合我的审美', !!feedback?.starred)} title={label('符合我的审美', !!feedback?.starred)} aria-pressed={!!feedback?.starred} disabled={blocked}
        className={`${base} ${feedback?.starred ? 'border-accent/50 bg-accent/15 text-accent' : 'border-border bg-surface text-secondary hover:text-accent'}`}
        onClick={() => { if (record) onChange(record, { starred: !record.feedback.starred, outcome: record.feedback.outcome }) }}><Star aria-hidden="true" className="h-5 w-5" fill={feedback?.starred ? 'currentColor' : 'none'} /></button>
      {(['profit', 'loss'] as const).map(outcome => {
        const selected = feedback?.outcome === outcome
        const color = outcome === 'profit'
          ? selected ? 'border-red-700 bg-red-700 text-white dark:border-red-500 dark:bg-red-600' : 'border-red-300 bg-red-50 text-red-800 dark:border-red-400/40 dark:bg-red-500/10 dark:text-red-300'
          : selected ? 'border-green-700 bg-green-700 text-white dark:border-green-500 dark:bg-green-700' : 'border-green-300 bg-green-50 text-green-800 dark:border-green-400/40 dark:bg-green-500/10 dark:text-green-300'
        return <button key={outcome} type="button" className={`${base} ${color}`} aria-label={label(outcome === 'profit' ? '我赚了' : '我亏了', selected)} title={label(outcome === 'profit' ? '我赚了' : '我亏了', selected)} aria-pressed={selected} disabled={blocked}
          onClick={() => { if (record) onChange(record, { starred: record.feedback.starred, outcome: selected ? null : outcome }) }}>{outcome === 'profit' ? '盈' : '亏'}</button>
      })}
    </div>
    {state?.pending && <p role="status" className="text-[11px] text-muted">保存中…</p>}
    {state?.error && <div role="alert" className="max-w-48 whitespace-normal text-xs text-danger">{state.error}{state.retry && <button className="ml-1 min-h-8 underline" onClick={onRetry}>重试保存</button>}</div>}
  </div>
}
