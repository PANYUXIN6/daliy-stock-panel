import { useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { api, type EveningReport as Report, type EveningRecord, type EveningReportStatus } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { eveningPerformanceQueryOptions, refreshEveningPerformance } from '@/lib/useSharedQueries'
import { useCurrentUser } from '@/lib/auth'
import { useCollectorJob } from '@/lib/useCollectorJob'
import { PageHeader } from '@/components/PageHeader'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { WatchlistAddMenu } from '@/components/WatchlistAddMenu'
import { EveningReportContent } from '@/components/evening-report/EveningReportContent'
import { EveningFeedbackButtons, useEveningFeedback } from '@/components/evening-report/EveningFeedbackButtons'
import { EveningPersonalNav } from '@/components/evening-report/EveningPersonalNav'

const panel = 'rounded-xl border border-border bg-surface p-4 sm:p-5'
const control = 'rounded border border-border bg-elevated px-3 py-2 text-xs text-foreground disabled:opacity-50'
const statuses: Record<string, string> = { waiting: '等待数据', ready: '可生成', generating: '生成中', published: '已发布', failed: '生成失败', cancelled: '已取消', unpublished: '未发布' }
function Status({ value }: { value: EveningReportStatus }) {
  return <div className="space-y-2 text-sm" role="status"><p className="font-medium">{value.date && `${value.date} · `}{statuses[value.status] ?? value.status}</p>{value.reason && <p>{value.reason}</p>}{value.progress !== undefined && <p>生成进度：{value.progress}%</p>}{!!value.missing_dependencies?.length && <p>{value.missing_dependencies.join('；')}</p>}{value.last_attempt_at && <p className="text-muted">最后尝试：{value.last_attempt_at}</p>}</div>
}

export function EveningReport() {
  const user = useCurrentUser()
  const qc = useQueryClient()
  const [searchParams] = useSearchParams()
  const [chosenDate, setDate] = useState(searchParams.get('date') ?? '')
  const [version, setVersion] = useState(searchParams.get('version') ?? '')
  const [submittedJob, setSubmittedJob] = useState<{ date: string; id: string } | null>(null)
  const latest = useQuery({ queryKey: QK.eveningReportStatus(), queryFn: ({ signal }) => api.eveningReportStatus('', signal), refetchInterval: query => query.state.data?.status === 'generating' ? 2_000 : 30_000 })
  const day = chosenDate || latest.data?.date || ''
  const report = useQuery({ queryKey: QK.eveningReport(day, version), queryFn: ({ signal }) => api.eveningReport(day, version, signal), enabled: !!day })
  const selectedStatus = useQuery({ queryKey: QK.eveningReportStatus(chosenDate), queryFn: ({ signal }) => api.eveningReportStatus(chosenDate, signal), enabled: !!chosenDate && chosenDate !== latest.data?.date, refetchInterval: query => query.state.data?.status === 'generating' ? 2_000 : 30_000 })
  const status = !chosenDate || chosenDate === latest.data?.date ? latest : selectedStatus
  const data = report.data && 'records' in report.data ? report.data : undefined
  const displayStatus = status.data ?? (report.data && !data ? report.data as EveningReportStatus : undefined)
  const refresh = () => { void qc.invalidateQueries({ queryKey: QK.eveningReportRoot }); if (user) void qc.invalidateQueries({ queryKey: QK.eveningPersonalRoot(user.id) }) }
  const job = useCollectorJob((submittedJob?.date === day ? submittedJob.id : null) ?? (user?.role === 'admin' ? status.data?.job_id ?? null : null), refresh)
  const rebuild = useMutation({
    mutationFn: api.eveningReportRebuild,
    onSuccess: (result, date) => {
      setSubmittedJob(result.job_id ? { date, id: result.job_id } : null)
      refresh()
    },
  })
  const loading = (!chosenDate && latest.isLoading) || (day && report.isLoading)
  return <div className="mx-auto min-w-0 max-w-[1600px] space-y-5 break-words p-4 pb-8 sm:p-6 sm:pb-10 lg:p-8">
    <PageHeader className="flex-col items-start px-0 pt-0 pb-4 sm:flex-row sm:items-center" title="每日晚报" right={
      <div className="flex w-full items-center gap-2 sm:w-auto">
        <label className="min-w-0 flex-1 sm:flex-none"><span className="sr-only">报告日期</span><input aria-label="报告日期" type="date" className={`${control} h-9 w-full tabular-nums [color-scheme:light] dark:[color-scheme:dark] sm:w-auto`} value={day} onChange={e => { setDate(e.target.value); setVersion(''); setSubmittedJob(null); rebuild.reset() }} /></label>
        <button className={`${control} inline-flex h-9 shrink-0 items-center gap-1.5`} onClick={refresh} aria-label="刷新晚报" aria-busy={report.isFetching}><RefreshCw className={`h-3.5 w-3.5 ${report.isFetching ? 'motion-safe:animate-spin' : ''}`} aria-hidden="true" />刷新</button>
      </div>
    } />
    <EveningPersonalNav />
    {(report.isError || status.isError) && <div role="alert" className={`${panel} text-danger`}>读取失败：{report.error?.message ?? status.error?.message}<button className={`${control} ml-3`} onClick={refresh}>重试读取</button></div>}
    {loading && <div className={`${panel} animate-pulse`} role="status">正在读取 {day || '最近交易日'} 晚报观察名单…</div>}
    {displayStatus && (displayStatus.status !== 'published' || !data) && <section className={panel}><Status value={displayStatus} /></section>}
    {user?.role === 'admin' && !data && status.data?.can_generate && <details className="rounded-lg border border-border px-4 py-3 text-xs"><summary className="cursor-pointer text-secondary">生成管理{job.running ? ' · 生成中' : ''}</summary>
      <div className="mt-3 space-y-3"><div className="flex flex-wrap items-center gap-3"><button className={control} disabled={rebuild.isPending || job.running || status.data?.status === 'generating'} onClick={() => rebuild.mutate(day)}>{rebuild.isPending ? '提交中…' : '生成晚报'}</button><span className="text-muted">北京时间16:00后可准备正式数据并生成。发布后直接读取定稿。</span></div>
      {rebuild.isError && <p role="alert" className="text-danger">提交失败：{rebuild.error.message}</p>}
      {job.data && <div role="status">任务：{job.data.outcome === 'cancelled' ? '已取消' : job.data.status === 'succeeded' ? '已完成' : job.data.status === 'failed' ? '失败' : '生成中'} · {job.data.progress}% · {job.message}{job.data.error && <p className="text-danger">{job.data.error}</p>}{job.running && <button className={`${control} ml-3`} disabled={job.cancel.isPending} onClick={() => job.cancel.mutate()}>取消生成</button>}</div>}
      {(job.isError || job.cancel.isError) && <p role="alert" className="text-danger">任务操作失败：{job.error?.message ?? job.cancel.error?.message}</p>}
    </div></details>}
    {data && <ReportContent key={`${user?.id}:${data.date}:${data.version}`} report={data} />}
  </div>
}

type AddState = { pending: boolean; group: string | null; error?: string }
function ReportContent({ report }: { report: Report }) {
  const qc = useQueryClient()
  const user = useCurrentUser()
  const personalKey = QK.eveningPersonal(user?.id ?? '', report.date, report.version)
  const personal = useQuery({ ...eveningPerformanceQueryOptions, queryKey: personalKey,
    queryFn: ({ signal }) => api.eveningPersonal(report.date, report.version, signal), enabled: !!user,
    refetchInterval: query => query.state.data?.ready === false ? 5_000 : false })
  const feedback = useEveningFeedback(user?.id ?? '')
  const records = personal.data && personal.data.user_id === user?.id ? personal.data.records : []
  const personalReady = !!personal.data?.ready && personal.data.user_id === user?.id
  const bySymbol = new Map(records.map(record => [record.symbol, record]))
  const [preview, setPreview] = useState<{ symbol: string; name?: string; list: { symbol: string; name: string }[] } | null>(null)
  const [adds, setAdds] = useState<Record<string, AddState>>({})
  // 同步锁避免重复点击改写自选加入信息。
  const inFlight = useRef(new Set<string>())
  const watchlist = useQuery({ queryKey: QK.watchlist, queryFn: api.watchlistList })
  const watched = new Set(watchlist.data?.symbols.map(s => s.symbol) ?? [])
  const add = async (row: EveningRecord, group: string | null) => {
    if (watched.has(row.symbol) || inFlight.current.has(row.symbol) || !watchlist.data) return
    inFlight.current.add(row.symbol)
    setAdds(previous => ({ ...previous, [row.symbol]: { pending: true, group } }))
    try {
      const result = await api.watchlistAdd(row.symbol, `每日晚报 ${report.trade_date}`, group)
      qc.setQueryData(QK.watchlist, result)
      await Promise.all([qc.invalidateQueries({ queryKey: QK.watchlist }), qc.invalidateQueries({ queryKey: ['watchlist-enriched'] })])
      setAdds(previous => ({ ...previous, [row.symbol]: { pending: false, group } }))
    } catch (error) { setAdds(previous => ({ ...previous, [row.symbol]: { pending: false, group, error: error instanceof Error ? error.message : '添加失败，请重试' } })) }
    finally { inFlight.current.delete(row.symbol) }
  }
  const open = (row: EveningRecord, list: EveningRecord[]) => setPreview({ symbol: row.symbol, name: row.name, list: list.map(r => ({ symbol: r.symbol, name: r.name })) })
  const actions = (row: EveningRecord, selected: EveningRecord[], closeDetails: () => void) => <div className="space-y-1 text-xs">
    <div className="flex flex-wrap gap-2"><button className={control} onClick={() => { closeDetails(); open(row, selected) }}>查看当前行情</button><WatchlistAddMenu disabled={watched.has(row.symbol) || adds[row.symbol]?.pending || !watchlist.data || watchlist.isError} preferredGroupId={adds[row.symbol]?.group} triggerClassName={control} onSelect={group => void add(row, group)} ariaLabel={`${row.name}加入自选`}>{watched.has(row.symbol) ? '已在自选' : adds[row.symbol]?.pending ? '添加中…' : '加入自选'}</WatchlistAddMenu></div>
    {adds[row.symbol]?.error && <div role="alert" className="text-danger">{adds[row.symbol].error}<button className="ml-2 underline" onClick={() => void add(row, adds[row.symbol].group)}>按原分组重试</button></div>}
  </div>
  return <>
    {personal.isError ? <p role="alert" className="text-sm text-danger">个人记录读取失败：{personal.error.message}<button className={`${control} ml-2`} onClick={() => void personal.refetch()}>重试个人记录</button></p> : !personalReady && <p role="status" className="text-xs text-secondary">{personal.isLoading ? '正在读取个人记录…' : '个人观察记录准备中，完成后可设置星标与盈亏。'}</p>}
    <EveningReportContent report={report} renderActions={actions} onOpenStock={open} personalRecords={records}
      onOpenDetails={user ? () => void refreshEveningPerformance(qc, personalKey) : undefined} renderFeedback={row => {
      const record = bySymbol.get(row.symbol)
      return <EveningFeedbackButtons name={row.name} record={record} state={record ? feedback.states[record.episode_id] : undefined} disabled={!personalReady || personal.isError} onChange={feedback.change} onRetry={() => record && feedback.retry(record.episode_id)} />
    }} />
    {watchlist.isError && <p role="alert" className="text-xs text-danger">自选状态加载失败，暂不能添加：{watchlist.error.message}<button className="ml-2 underline" onClick={() => void watchlist.refetch()}>重试加载自选</button></p>}
    <StockPreviewDialog symbol={preview?.symbol ?? null} name={preview?.name} navList={preview?.list} onNavigate={(symbol, name) => setPreview(previous => previous ? { ...previous, symbol, name } : null)} onClose={() => setPreview(null)} />
  </>
}
