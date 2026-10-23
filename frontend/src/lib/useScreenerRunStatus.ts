import { useCallback, useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type ScreenerCachedSummary, type ScreenerRunAllSummary } from './api'
import { QK } from './queryKeys'

type Ticket = { scope: string; sequence: number }
type Run = {
  ticket: Ticket
  requested: string[]
  runId?: string
  pending: string[]
  startedAt: number
  active: boolean
  errors: Record<string, string>
  error: string | null
  notice: string | null
}

/** Keep each asset/date's task while another scope is visible; only a newer run supersedes it. */
export function useScreenerRunStatus(scope: string, asOf: string) {
  const qc = useQueryClient()
  const visibleScope = useRef(scope)
  visibleScope.current = scope
  const sequence = useRef(0)
  const tickets = useRef(new Map<string, Ticket>())
  const [runs, setRuns] = useState<Record<string, Run>>({})
  const isLatest = useCallback((ticket: Ticket) => tickets.current.get(ticket.scope) === ticket, [])
  const current = runs[scope]
  const published = useRef('')

  const begin = useCallback((requested: string[]) => {
    const runScope = visibleScope.current
    const previousTicket = tickets.current.get(runScope)
    const ticket = { scope: runScope, sequence: ++sequence.current }
    tickets.current.set(runScope, ticket)
    setRuns(previous => {
      const saved = previous[runScope]
      const errors: Record<string, string> = {}
      if (saved && saved.ticket === previousTicket) {
        Object.assign(errors, saved.errors)
        if (saved.error) for (const id of saved.requested) errors[id] ??= saved.error
        for (const id of requested) delete errors[id]
      }
      return { ...previous, [runScope]: {
        ticket, requested, pending: [], startedAt: 0, active: false, errors, error: null, notice: null,
      } }
    })
    return ticket
  }, [])

  const accept = useCallback((ticket: Ticket, data: ScreenerRunAllSummary, confirmedScope = ticket.scope) => {
    if (!isLatest(ticket)) return null
    const destination = tickets.current.get(confirmedScope)
    // A response for an unspecified date cannot replace a newer explicit run of that date.
    if (destination && destination.sequence > ticket.sequence) return null
    const acceptedTicket = confirmedScope === ticket.scope ? ticket : { ...ticket, scope: confirmedScope }
    if (confirmedScope !== ticket.scope) tickets.current.delete(ticket.scope)
    tickets.current.set(confirmedScope, acceptedTicket)
    const pending = (data.pending ?? []).filter(id => !data.errors?.[id])
    setRuns(previous => {
      const saved = previous[ticket.scope]
      if (saved?.ticket !== ticket) return previous
      const next = { ...previous }
      if (confirmedScope !== ticket.scope) delete next[ticket.scope]
      next[confirmedScope] = {
        ...saved, ticket: acceptedTicket, runId: data.run_id, pending, startedAt: data.started_at ?? 0,
        active: !!pending.length, errors: { ...saved.errors, ...data.errors }, error: data.error ?? null,
      }
      return next
    })
    void qc.invalidateQueries({ queryKey: QK.screenerCachedRoot })
    // Hidden responses still migrate their queued requests; only visible ones
    // may replace the page's selected date and counts.
    return { scope: confirmedScope, visible: visibleScope.current === ticket.scope || visibleScope.current === confirmedScope }
  }, [isLatest, qc])

  const reject = useCallback((ticket: Ticket, error: unknown) => {
    if (!isLatest(ticket)) return
    setRuns(previous => previous[ticket.scope]?.ticket === ticket ? {
      ...previous, [ticket.scope]: {
        ...previous[ticket.scope], active: false, pending: [], error: error instanceof Error ? error.message : String(error),
      },
    } : previous)
  }, [isLatest])

  const query = useQuery({
    queryKey: QK.screenerRunStatus(current?.runId ?? ''),
    queryFn: () => api.screenerRunStatus(current!.runId!),
    enabled: !!current?.runId && current.active,
    retry: false,
    refetchInterval: state => state.state.data?.done ? false : 2000,
  })
  const runId = current?.runId
  const ticket = current?.ticket
  useEffect(() => {
    const data = query.data
    if (!ticket || !runId || !data || data.run_id !== runId || !isLatest(ticket)) return
    const signature = JSON.stringify(data)
    if (published.current !== signature) {
      published.current = signature
      void qc.invalidateQueries({ queryKey: QK.screenerCachedRoot })
    }
    setRuns(previous => previous[ticket.scope]?.ticket === ticket ? {
      ...previous, [ticket.scope]: {
        ...previous[ticket.scope], pending: data.done ? [] : data.pending.filter(id => !data.errors[id]),
        active: !data.done, errors: { ...previous[ticket.scope].errors, ...data.errors }, error: data.error,
      },
    } : previous)
  }, [query.data, runId, ticket, isLatest, qc])

  useEffect(() => {
    if (!ticket || !runId || !query.error || (query.error as { status?: number }).status !== 404 || !isLatest(ticket)) return
    setRuns(previous => previous[ticket.scope]?.ticket === ticket ? {
      ...previous, [ticket.scope]: {
        ...previous[ticket.scope], pending: [], active: false,
        notice: '任务状态已过期或服务已重启，请刷新结果后重试；无法确认此次计算是否完成。',
      },
    } : previous)
    void qc.invalidateQueries({ queryKey: QK.screenerCachedRoot })
  }, [query.error, runId, ticket, isLatest, qc])

  // Compatibility with older servers: only their tasks use result arrival as a fallback.
  const reconcileSummary = useCallback((summary: ScreenerCachedSummary) => {
    setRuns(previous => {
      const saved = previous[scope]
      if (!saved || !isLatest(saved.ticket) || !saved.active || saved.runId) return previous
      const pending = saved.pending.filter(id => {
        const result = summary.results[id]
        return !result || result.as_of !== asOf
          || (result.computed_at != null && result.computed_at < saved.startedAt - 2000)
      })
      return pending.length === saved.pending.length ? previous : {
        ...previous, [scope]: { ...saved, pending, active: !!pending.length },
      }
    })
  }, [scope, asOf, isLatest])

  useEffect(() => {
    if (!current?.active || current.runId) return
    const ticket = current.ticket
    const timer = setTimeout(() => {
      if (!isLatest(ticket)) return
      setRuns(previous => previous[ticket.scope]?.ticket === ticket ? {
        ...previous, [ticket.scope]: {
          ...previous[ticket.scope], active: false, pending: [], notice: '暂时无法确认后台计算状态，请刷新结果后重试；任务未被取消。',
        },
      } : previous)
    }, 8 * 60 * 1000)
    return () => clearTimeout(timer)
  }, [current?.active, current?.runId, ticket, isLatest])

  return {
    begin, accept, reject, reconcileSummary,
    requestedIds: current?.requested ?? [],
    pendingIds: current?.pending ?? [], running: !!current?.active,
    errors: current?.errors ?? {}, error: current?.error ?? null,
    retryIds: [...new Set([
      ...Object.keys(current?.errors ?? {}),
      ...(current?.error || current?.notice ? current.requested : []),
    ])],
    notice: current?.notice ?? (current?.active && query.isError
      ? '任务状态暂时获取失败，正在重试；后台计算未被取消。' : null),
  }
}
