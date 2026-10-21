import { useRef, useMemo, useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import { Ban, Loader2, CheckCircle2, XCircle, X } from 'lucide-react'
import { formatDuration, formatLogTime } from '@/lib/format'
import { Pill } from './StatCard'
import type { PipelineJob, PipelineTaskStatus } from '@/lib/api'
import { pipelineOutcome } from '@/lib/pipelineFeedback'

export const STAGE_LABELS: Record<string, string> = {
  init: '初始化',
  resolve_universe: '解析标的池',
  sync_instruments: '同步个股维表',
  sync_daily: '同步日 K',
  sync_adj: '同步除权因子',
  compute_enriched: '计算技术指标',
  sync_index: '同步核心指数与 ETF',
  prefetch_index: '预取核心指数',
  prefetch_concept: '预取概念分类',
  prefetch_industry: '预取行业分类',
  prefetch_auction: '预取竞价风向标',
  prefetch_dragon_tiger: '预取龙虎榜',
  verify_postmarket: '核验当日行情与指标',
  sync_classifications: '同步概念与行业分类',
  compute_regime: '计算市场环境与情绪周期',
  compute_mainline: '计算概念与行业主线',
  paper_settle: '模拟盘结算',
  depth_finalize: '更新盘后盘口',
  sync_boards: '更新竞价与龙虎榜',
  publish_review: '发布市场复盘快照',
  sync_minute: '同步分钟 K',
  financials: '同步财务数据',
  classifications: '同步板块分类',
  extend_history: '扩展日K历史',
  extend_minute: '扩展分钟K历史',
  rebuild_enriched: '全量计算',
  refresh_views: '刷新视图',
  done: '完成',
}

const TASK_STATUS_LABELS: Record<PipelineTaskStatus, string> = {
  pending: '等待中', running: '运行中', succeeded: '完成', failed: '失败', skipped: '跳过', cancelled: '已取消',
}

function elapsedSeconds(start: string | null | undefined, now: number): number | null {
  if (!start) return null
  const epoch = Date.parse(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(start) ? start : `${start}Z`)
  return Number.isFinite(epoch) ? Math.max(0, Math.floor((now - epoch) / 1000)) : null
}

function LogViewer({ log }: { log: PipelineJob['log'] }) {
  const containerRef = useRef<HTMLDivElement>(null)

  const displayLog = useMemo(() => {
    if (!log.length) return []
    return log.filter((line, i) => {
      const next = log[i + 1]
      if (
        next &&
        line.stage === next.stage &&
        /\d+\/\d+/.test(line.msg) &&
        /\d+\/\d+/.test(next.msg)
      ) {
        return false
      }
      return true
    })
  }, [log])

  useEffect(() => {
    const el = containerRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [displayLog])

  return (
    <div ref={containerRef} className="rounded-btn bg-base/60 border border-border max-h-48 overflow-y-auto px-3 py-2 font-mono text-[11px] space-y-0.5">
      {displayLog.map((line, i) => (
        <div key={`${line.ts}-${i}`} className="flex gap-2 text-secondary">
          <span className="text-muted shrink-0">{formatLogTime(line.ts)}</span>
          <span className="text-accent/70 shrink-0">[{STAGE_LABELS[line.stage] ?? line.stage}]</span>
          <span>{line.msg}</span>
        </div>
      ))}
      {displayLog.length === 0 && <div className="text-muted">等待启动…</div>}
    </div>
  )
}

export function ActiveJobCard({ job, onDismiss }: { job: PipelineJob; onDismiss?: () => void }) {
  const isDone = job.status === 'succeeded' || job.status === 'failed'
  const outcome = pipelineOutcome(job)
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    if (isDone) return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [isDone])
  const statusMap = {
    running:   { icon: Loader2,     color: 'text-accent',   label: '运行中', spinning: true,  border: 'border-accent/40', bg: 'bg-accent/5' },
    pending:   { icon: Loader2,     color: 'text-muted',    label: '排队中', spinning: true,  border: 'border-border',    bg: 'bg-surface' },
    succeeded: { icon: CheckCircle2, color: 'text-bear',     label: '完成',   spinning: false, border: 'border-bear/30',   bg: 'bg-bear/5' },
    failed:    { icon: XCircle,     color: 'text-danger',   label: '失败',   spinning: false, border: 'border-danger/40', bg: 'bg-danger/5' },
    cancelled: { icon: Ban,         color: 'text-muted',    label: '已取消', spinning: false, border: 'border-border', bg: 'bg-surface' },
  } as const
  const meta = statusMap[outcome === 'cancelled' ? 'cancelled' : job.status]
  const Icon = meta.icon
  const stageLabel = isDone ? meta.label : (STAGE_LABELS[job.stage] ?? job.stage)
  const tasks = Object.entries(job.tasks ?? {})
  const duration = job.duration_s ?? elapsedSeconds(job.started_at, now)
  const updated = job.last_progress_at ?? job.log?.at(-1)?.ts
  const updateAge = elapsedSeconds(updated, now)

  return (
    <div className={`rounded-card border ${meta.border} ${meta.bg} p-5`}>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2.5">
          <Icon className={`h-5 w-5 ${meta.color} ${meta.spinning ? 'animate-spin' : ''}`} />
          <div>
            <div className="text-sm font-medium text-foreground">
              {meta.label}{!isDone && ` · ${stageLabel}`}
            </div>
            <div className="text-xs text-secondary font-mono mt-0.5">
              {job.id} · {duration != null ? `${isDone ? '耗时' : '已用'} ${formatDuration(Math.round(duration))}` : '等待启动'}
            </div>
          </div>
        </div>
        {!isDone && (
          <div className="font-mono text-2xl font-bold tracking-tight">
            {job.progress}<span className="text-[16px] leading-6 text-muted">%</span>
          </div>
        )}
        {isDone && onDismiss && <button type="button" aria-label="关闭任务详情" title="关闭详情，仍可在同步历史查看" onClick={onDismiss} className="rounded p-1 text-muted hover:text-foreground"><X className="h-4 w-4" /></button>}
      </div>

      {!isDone && (
        <div className="mb-2">
          <div className="h-1.5 rounded-full bg-elevated overflow-hidden">
            <motion.div
              className="h-full bg-accent"
              animate={{ width: `${job.progress}%` }}
              transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
            />
          </div>
          <p className="mt-1 text-[10px] text-muted">总体进度按阶段推进，不代表剩余时间。</p>
          {job.stage_pct > 0 && (
            <div className="flex items-center justify-between mt-1">
              <span className="text-[10px] text-muted">当前阶段</span>
              <span className="text-[10px] font-mono text-secondary">{job.stage_pct}%</span>
            </div>
          )}
        </div>
      )}

      {updated && <div className="mb-3 text-[10px] text-muted">最后更新 {formatLogTime(updated)}{!isDone && updateAge != null ? ` · ${updateAge} 秒前` : ''}</div>}

      {tasks.length > 0 && <ul aria-label="各项任务进度" className="mb-3 grid gap-2 sm:grid-cols-2">
        {tasks.map(([id, task]) => {
          const taskDone = !['pending', 'running'].includes(task.status)
          const seconds = taskDone ? task.duration_s : elapsedSeconds(task.started_at, now) ?? task.duration_s
          const color = task.status === 'failed' ? 'text-danger' : task.status === 'succeeded' ? 'text-emerald-400' : task.status === 'running' ? 'text-accent' : 'text-muted'
          return <li key={id} className="rounded-btn border border-border bg-base/40 px-3 py-2 text-[11px]">
            <div className="flex items-center justify-between gap-2">
              <span className="text-foreground">{STAGE_LABELS[id] || task.label || id}</span>
              <span className={color}>{TASK_STATUS_LABELS[task.status]}{task.status === 'running' && task.progress != null ? ` ${task.progress}%` : ''}</span>
            </div>
            <div className="mt-1 flex flex-wrap gap-x-3 font-mono text-muted">
              {seconds != null && <span>{taskDone ? '耗时' : '已用'} {formatDuration(Math.round(seconds))}</span>}
              {task.last_progress_at && <span>更新于 {formatLogTime(task.last_progress_at)}</span>}
            </div>
            {(task.message || task.error) && <div className={`mt-1 break-words ${task.error ? 'text-danger' : 'text-secondary'}`}>{task.error || task.message}</div>}
          </li>
        })}
      </ul>}

      {tasks.length === 0 && Object.keys(job.stage_timings ?? {}).length > 0 && <details className="mb-3 text-[11px] text-secondary">
        <summary className="cursor-pointer text-muted">阶段耗时</summary>
        <ul className="mt-1 space-y-1">{Object.entries(job.stage_timings ?? {}).map(([stage, seconds]) => <li key={stage}>{STAGE_LABELS[stage] ?? stage} · {formatDuration(Math.round(seconds))}</li>)}</ul>
      </details>}

      <LogViewer log={job.log ?? []} />

      {job.status === 'succeeded' && job.result && (() => {
        const skipped = new Set(job.result.skipped_stages ?? [])
        const cell = (stage: string | null, v: string) =>
          stage && skipped.has(stage) ? '跳过' : v
        // 按结果里实际存在的字段渲染: 独立除权因子同步没有日K/分钟K字段, 不显示 "0 天"
        const pills: { label: string; value: string }[] = []
        if (job.result.universe_size != null) pills.push({ label: '标的池', value: String(job.result.universe_size) })
        if (job.result.daily_days != null) pills.push({ label: '日 K', value: cell(null, `${job.result.daily_days} 天`) })
        if (job.result.adj_factor_symbols != null) pills.push({ label: '除权因子', value: cell('sync_adj', `${job.result.adj_factor_symbols} 只`) })
        if (job.result.enriched_days != null) pills.push({ label: 'enriched', value: cell(null, `${job.result.enriched_days} 行`) })
        if (job.result.minute_rows != null) pills.push({ label: '分钟K', value: cell('sync_minute', `${job.result.minute_rows} 行`) })
        if (pills.length === 0) return null
        return (
          <div className="mt-3 grid grid-cols-2 md:grid-cols-5 gap-3 text-xs">
            {pills.map(p => <Pill key={p.label} {...p} />)}
          </div>
        )
      })()}
      {job.status === 'failed' && job.error && (
        <div className={`mt-3 rounded-btn border px-3 py-2 text-xs ${outcome === 'cancelled' ? 'border-border text-muted' : 'border-danger/40 bg-danger/5 text-danger'}`}>
          {job.error}
          {job.error.includes('超时自动取消') && (
            <div className="mt-1 text-[10px] text-muted">
              判定依据是「无进度」而非总时长, 任务只要仍在推进就不会被中断;
              若网络环境较慢可在 设置 → 网络设置 中调大停滞阈值。
            </div>
          )}
        </div>
      )}
    </div>
  )
}
