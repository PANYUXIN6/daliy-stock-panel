import { useEffect, useRef, useState } from 'react'
import { Ban, CheckCircle2, Loader2, XCircle } from 'lucide-react'
import type { PipelineJobSummary } from '@/lib/api'
import { pipelineOutcome, type PipelineOutcome } from '@/lib/pipelineFeedback'

export function DataSyncIndicator({ activeId, jobs }: { activeId: string | null; jobs: PipelineJobSummary[] }) {
  const observedId = useRef<string | null>(null)
  const [feedback, setFeedback] = useState<{ outcome: PipelineOutcome; error: string | null } | null>(null)

  useEffect(() => {
    if (activeId) {
      observedId.current = activeId
      setFeedback(null)
      return
    }
    // 状态列表有短暂落后时，保留任务 id，等取到它的真实终态再提示。
    if (!observedId.current) return
    const finished = jobs.find(job => job.id === observedId.current)
    if (!finished) return
    const outcome = pipelineOutcome(finished)
    if (!outcome) return
    observedId.current = null
    setFeedback({ outcome, error: finished.error })
  }, [activeId, jobs])

  useEffect(() => {
    if (!feedback) return
    const timer = setTimeout(() => setFeedback(null), 5000)
    return () => clearTimeout(timer)
  }, [feedback])

  if (activeId) return <span role="status" aria-label="数据同步中" title="数据同步中，点击查看进度"><Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-accent" /></span>
  if (!feedback) return null
  const display = {
    succeeded: { Icon: CheckCircle2, label: '数据同步完成', color: 'text-emerald-400' },
    failed: { Icon: XCircle, label: '数据同步失败', color: 'text-danger' },
    cancelled: { Icon: Ban, label: '数据同步已取消', color: 'text-muted' },
  }[feedback.outcome]
  const Icon = display.Icon
  return <span role="status" aria-label={display.label} title={`${display.label}${feedback.error ? `：${feedback.error}` : ''}，点击查看记录`}><Icon className={`h-3.5 w-3.5 shrink-0 ${display.color}`} /></span>
}
