import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { api, type EveningReport as Report, type EveningRecord, type EveningReportStatus } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useCurrentUser } from '@/lib/auth'
import { useCollectorJob } from '@/lib/useCollectorJob'
import { PageHeader } from '@/components/PageHeader'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { WatchlistAddMenu } from '@/components/WatchlistAddMenu'
import { EveningReportContent } from '@/components/evening-report/EveningReportContent'

const panel = 'rounded-xl border border-border bg-surface p-4 sm:p-5'
const control = 'rounded border border-border bg-elevated px-3 py-2 text-xs text-foreground disabled:opacity-50'
const statuses: Record<string, string> = { waiting: '等待数据', ready: '可生成', generating: '生成中', published: '已发布', failed: '生成失败', cancelled: '已取消' }
function Status({ value }: { value: EveningReportStatus }) {
  return <div className="space-y-2 text-sm" role="status"><p className="font-medium">{value.date} · {statuses[value.status] ?? value.status}</p>{value.reason && <p>{value.reason}</p>}{value.progress !== undefined && <p>生成进度：{value.progress}%</p>}{!!value.missing_dependencies?.length && <p>{value.missing_dependencies.join('；')}</p>}{value.last_attempt_at && <p className="text-muted">最后尝试：{value.last_attempt_at}</p>}</div>
}

export function EveningReport() {
  const user = useCurrentUser()
  const qc = useQueryClient()
  const [chosenDate, setDate] = useState('')
  const [version, setVersion] = useState('')
  const [submittedJob, setSubmittedJob] = useState<{ date: string; id: string } | null>(null)
  const dates = useQuery({ queryKey: QK.eveningReportDates(), queryFn: ({ signal }) => api.eveningReportDates(signal) })
  const today = useQuery({ queryKey: QK.eveningReportStatus(), queryFn: ({ signal }) => api.eveningReportStatus('', signal), refetchInterval: 30_000 })
  const day = chosenDate || dates.data?.dates[0]?.date || today.data?.date || ''
  const report = useQuery({ queryKey: QK.eveningReport(day, version), queryFn: ({ signal }) => api.eveningReport(day, version, signal), enabled: !!day })
  const status = useQuery({ queryKey: QK.eveningReportStatus(day), queryFn: ({ signal }) => api.eveningReportStatus(day, signal), enabled: !!day, refetchInterval: query => query.state.data?.status === 'generating' ? 2_000 : 30_000 })
  const data = report.data && 'records' in report.data ? report.data : undefined
  const refresh = () => { void qc.invalidateQueries({ queryKey: QK.eveningReportRoot }) }
  const job = useCollectorJob((submittedJob?.date === day ? submittedJob.id : null) ?? (user?.role === 'admin' ? status.data?.job_id ?? null : null), refresh)
  const rebuild = useMutation({ mutationFn: (request: { date: string; mode: 'live' | 'revision' }) => api.eveningReportRebuild(request.date, request.mode), onSuccess: (result, request) => { setSubmittedJob({ date: request.date, id: result.job_id }); refresh() } })
  const versions = dates.data?.dates.find(d => d.date === day)?.versions ?? []
  const loading = dates.isLoading || (day && report.isLoading)
  return <div className="space-y-5 pb-8 min-w-0 break-words">
    <PageHeader title="每日晚报" right={<button className={control} onClick={refresh}><RefreshCw className="mr-1 inline h-3.5 w-3.5" />刷新</button>} />
    <section className={panel}>
      <div className="flex flex-wrap items-end gap-4">
        <label className="space-y-1 text-xs"><span className="block text-muted">报告日期</span><input aria-label="报告日期" type="date" className={control} value={day} onChange={e => { setDate(e.target.value); setVersion(''); setSubmittedJob(null); rebuild.reset() }} /></label>
        <label className="min-w-0 space-y-1 text-xs"><span className="block text-muted">报告版本</span><select className={`${control} max-w-full`} aria-label="报告版本" value={version} onChange={e => { setVersion(e.target.value); setSubmittedJob(null); rebuild.reset() }}><option value="">原发布</option>{versions.map(v => <option key={v.version} value={v.version}>{v.kind === 'revision' ? '修订' : '原发布'} · {v.generated_at}</option>)}</select></label>
        <span className="text-xs text-muted">实时归档 · {day || '正在确定最新已发布日期'}</span>
        {report.isFetching && data && <span role="status" className="text-xs text-accent">刷新中，保留当前完整报告</span>}
      </div>
      {today.data && today.data.status !== 'published' && <div className="mt-4 border-t border-border pt-3"><p className="mb-2 text-xs font-medium text-warning">今日{today.data.status === 'waiting' ? '等待数据' : '生成状态'}（不改变已发布报告日期）</p><Status value={today.data} /></div>}
    </section>
    {(dates.isError || report.isError || today.isError || status.isError) && <div role="alert" className={`${panel} text-danger`}>读取失败：{dates.error?.message ?? report.error?.message ?? today.error?.message ?? status.error?.message}<button className={`${control} ml-3`} onClick={refresh}>重试读取</button></div>}
    {loading && <div className={`${panel} animate-pulse`} role="status">正在读取 {day || '最新已发布'} 晚报观察名单…</div>}
    {!loading && !data && report.data && <section className={panel}><Status value={report.data as EveningReportStatus} /></section>}
    {status.data && (status.data.status !== 'published' || !data) && <section className={panel}><Status value={status.data} /></section>}
    {user?.role === 'admin' && day && <details className="rounded-lg border border-border px-4 py-3 text-xs"><summary className="cursor-pointer text-secondary">生成管理{job.running ? ' · 生成中' : ''}</summary>
      <div className="mt-3 space-y-3"><div className="flex flex-wrap items-center gap-3"><button className={control} disabled={rebuild.isPending || job.running || status.data?.status === 'generating'} onClick={() => rebuild.mutate({ date: day, mode: data ? 'revision' : 'live' })}>{rebuild.isPending ? '提交中…' : data ? '生成修订版' : '重试生成'}</button><span className="text-muted">由管理员显式提交；原发布保持可读。</span></div>
      {rebuild.isError && <p role="alert" className="text-danger">提交失败：{rebuild.error.message}</p>}
      {job.data && <div role="status">任务：{job.data.outcome === 'cancelled' ? '已取消' : job.data.status === 'succeeded' ? '已完成' : job.data.status === 'failed' ? '失败' : '生成中'} · {job.data.progress}% · {job.message}{job.data.error && <p className="text-danger">{job.data.error}</p>}{job.running && <button className={`${control} ml-3`} disabled={job.cancel.isPending} onClick={() => job.cancel.mutate()}>取消生成</button>}</div>}
      {(job.isError || job.cancel.isError) && <p role="alert" className="text-danger">任务操作失败：{job.error?.message ?? job.cancel.error?.message}</p>}
    </div></details>}
    {data && <ReportContent key={`${data.date}:${data.version}`} report={data} />}
  </div>
}

type AddState = { pending: boolean; group: string | null; error?: string }
function ReportContent({ report }: { report: Report }) {
  const qc = useQueryClient()
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
  const actions = (row: EveningRecord, selected: EveningRecord[]) => <div className="space-y-1 text-xs">
    <div className="flex flex-wrap gap-2"><button className={control} onClick={() => open(row, selected)}>查看当前行情</button><WatchlistAddMenu disabled={watched.has(row.symbol) || adds[row.symbol]?.pending || !watchlist.data || watchlist.isError} preferredGroupId={adds[row.symbol]?.group} triggerClassName={control} onSelect={group => void add(row, group)} ariaLabel={`${row.name}加入自选`}>{watched.has(row.symbol) ? '已在自选' : adds[row.symbol]?.pending ? '添加中…' : '加入自选'}</WatchlistAddMenu></div>
    {adds[row.symbol]?.error && <div role="alert" className="text-danger">{adds[row.symbol].error}<button className="ml-2 underline" onClick={() => void add(row, adds[row.symbol].group)}>按原分组重试</button></div>}
  </div>
  return <>
    <EveningReportContent report={report} renderActions={actions} />
    {watchlist.isError && <p role="alert" className="text-xs text-danger">自选状态加载失败，暂不能添加：{watchlist.error.message}<button className="ml-2 underline" onClick={() => void watchlist.refetch()}>重试加载自选</button></p>}
    <StockPreviewDialog symbol={preview?.symbol ?? null} name={preview?.name} navList={preview?.list} onNavigate={(symbol, name) => setPreview(previous => previous ? { ...previous, symbol, name } : null)} onClose={() => setPreview(null)} />
  </>
}
