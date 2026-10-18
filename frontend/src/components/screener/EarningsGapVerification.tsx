import { useEffect, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type EarningsGapVerificationSummary } from '@/lib/api'
import { useCurrentUser } from '@/lib/auth'
import { useCollectorJob } from '@/lib/useCollectorJob'
import { CollectorJobProgress } from '@/components/CollectorJobProgress'

export const EARNINGS_GAP_STRATEGY_ID = 'earnings_breakout_gap'

interface VerificationScope {
  asOf: string
  latestDate?: string | null
  assetType: 'stock' | 'etf'
  verification?: EarningsGapVerificationSummary
}

export function useEarningsGapVerification({ asOf, latestDate, assetType, verification }: VerificationScope) {
  const user = useCurrentUser()
  const qc = useQueryClient()
  const scope = `${user?.id ?? ''}:${user?.role ?? ''}:${assetType}:${asOf}:${latestDate ?? ''}`
  const scopeRef = useRef(scope)
  scopeRef.current = scope
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  const historical = !!latestDate && !!asOf && asOf < latestDate
  const canVerify = user?.role === 'admin' && assetType === 'stock' && !!asOf && asOf === latestDate
  const [submitted, setSubmitted] = useState<{ scope: string; jobId: string } | null>(null)
  const inFlightScope = useRef<string | null>(null)
  const mutation = useMutation({
    mutationFn: ({ date }: { date: string; scope: string }) => api.screenerVerifyEarningsGap(date),
    onSuccess: (data, variables) => {
      if (!mounted.current || scopeRef.current !== variables.scope) return
      setSubmitted({ scope: variables.scope, jobId: data.job_id })
      void qc.invalidateQueries({ queryKey: ['screener-cached'] })
    },
    onSettled: (_data, _error, variables) => {
      if (inFlightScope.current === variables.scope) inFlightScope.current = null
    },
  })
  const jobId = canVerify
    ? (submitted?.scope === scope ? submitted.jobId :
      verification?.status === 'running' ? verification.job_id ?? null : null)
    : null
  const job = useCollectorJob(jobId, () => {
    void qc.invalidateQueries({ queryKey: ['screener-cached'] })
    void qc.invalidateQueries({ queryKey: ['financials'] })
    void qc.invalidateQueries({ queryKey: ['earnings-breakout'] })
  })
  // 进度变化只重读本地候选快照；轮询和缓存查询均不会触发供应商请求。
  useEffect(() => {
    if (jobId && job.data?.status === 'running') {
      void qc.invalidateQueries({ queryKey: ['screener-cached'] })
    }
  }, [jobId, job.data?.status, job.data?.stage, job.data?.progress, qc])
  const submitting = mutation.isPending && mutation.variables?.scope === scope
  const start = () => {
    if (!canVerify || job.running || inFlightScope.current === scope) return
    inFlightScope.current = scope
    mutation.mutate({ date: asOf, scope })
  }
  return {
    start,
    busy: submitting || job.running,
    canVerify,
    historical,
    error: mutation.isError && mutation.variables?.scope === scope ? mutation.error : null,
    job,
  }
}

function formatCheckedAt(value: string) {
  const parsed = new Date(value)
  if (!Number.isFinite(parsed.getTime())) return '—'
  return parsed.toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  })
}

export function EarningsGapVerification({ verification, job, submitting, canVerify, historical = false, error }: {
  verification?: EarningsGapVerificationSummary
  job: ReturnType<typeof useCollectorJob>
  submitting: boolean
  canVerify: boolean
  historical?: boolean
  error?: Error | null
}) {
  const taskError = job.data?.status === 'failed' ? job.data.error ?? '核验失败，请重新点击策略卡片重试。' : null
  const completed = job.data?.status === 'succeeded' || verification?.status === 'completed'
  const showCompleted = completed && !job.running && !submitting && !error && !taskError
  return (
    <div className="rounded-card border border-border bg-surface p-4 space-y-3" aria-label="业绩断层核验">
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
        <h2 className="font-medium text-foreground">业绩断层核验</h2>
        <span className="text-accent tabular-nums">已确认 {verification?.confirmed_count ?? 0}</span>
      </div>
      <CollectorJobProgress job={{ ...job, message: job.data?.status === 'pending' ? '核验任务排队中…' : '正在核验利润表…' }} />
      {submitting && !job.running && <p role="status" className="text-xs text-accent">正在启动业绩断层核验…</p>}
      {error && <p role="alert" className="text-xs text-danger">{error.message}</p>}
      {taskError && <p role="alert" className="text-xs text-danger">{taskError}</p>}
      {showCompleted && <p role="status" className="text-[11px] text-secondary">核验完成，仅展示已确认的业绩断层跳空。</p>}
      {verification?.status === 'failed' && !taskError && <p role="alert" className="text-xs text-danger">上次核验未完成，可重新点击策略卡片重试。</p>}
      {showCompleted && verification?.checked_at && <p className="text-[11px] text-muted">完成时间（北京时间）：{formatCheckedAt(verification.checked_at)}</p>}
      {historical
        ? <p className="text-[11px] text-muted">历史结果按财报版本可用时间回放，无法证明当时可用的数据不会用于确认信号。</p>
        : !canVerify && <p className="text-[11px] text-muted">运行策略时按需准备财务数据，保存后共享复用。</p>}
      {!verification && !submitting && !job.running && !historical && <p className="text-xs text-secondary">点击策略卡片，核验当日业绩断层跳空。</p>}
    </div>
  )
}
