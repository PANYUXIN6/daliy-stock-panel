import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api, type EveningPersonalHistory, type EveningPersonalRecord } from '@/lib/api'
import { useCurrentUser } from '@/lib/auth'
import { QK } from '@/lib/queryKeys'
import { cnDateFromUtc, cnDateTimeFromUtc, fmtPct, priceColorClass } from '@/lib/format'
import type { ColumnConfig } from '@/lib/list-columns'
import { PageHeader } from '@/components/PageHeader'
import { StockDataTable } from '@/components/stock-table/StockDataTable'
import { useTableSort } from '@/components/stock-table/useTableSort'
import { EveningPersonalNav } from '@/components/evening-report/EveningPersonalNav'
import { EveningFeedbackButtons, useEveningFeedback } from '@/components/evening-report/EveningFeedbackButtons'
import { EveningFilters, emptyEveningFilters, eveningControl, matchesEveningFilters, roleLabels } from '@/components/evening-report/EveningFilters'
import { EveningObservationDetails, EveningPerformanceDetails } from '@/components/evening-report/EveningObservationDetails'

const columns: ColumnConfig[] = [
  { id: 'day', label: '推荐日期', source: { type: 'computed', key: 'day' }, visible: true },
  { id: 'stock', label: '股票', source: { type: 'builtin', key: 'symbol' }, visible: true },
  { id: 'theme', label: '题材 / 角色', source: { type: 'computed', key: 'theme' }, visible: true },
  ...(['1', '3', '5'] as const).map((key): ColumnConfig => ({ id: key, label: `D+${key}价格表现`, source: { type: 'computed', key }, visible: true, align: 'right' })),
  { id: 'detail', label: '跟踪过程', source: { type: 'computed', key: 'detail' }, visible: true },
  { id: 'feedback', label: '我的记录', source: { type: 'computed', key: 'feedback' }, visible: true },
]
const sortable = new Set(['day', '1', '3', '5'])
function sortValue(record: EveningPersonalRecord, column: ColumnConfig) {
  if (column.id === 'day') return record.day
  const performance = record.performance[column.id as '1' | '3' | '5']
  return performance?.status === 'evaluated' ? performance.return : null
}
const reportUrl = (record: EveningPersonalRecord) => `/evening-report?${new URLSearchParams({ date: record.day, version: record.version })}`

export function EveningHistory() {
  const user = useCurrentUser()
  const [month, setMonth] = useState(() => cnDateFromUtc(new Date().toISOString()).slice(0, 7))
  const query = useQuery({ queryKey: QK.eveningHistory(user?.id ?? '', month), queryFn: ({ signal }) => api.eveningHistory(month, signal), enabled: !!user && !!month })
  return <div className="mx-auto min-w-0 max-w-[1600px] space-y-5 p-4 pb-8 sm:p-6 sm:pb-10 lg:p-8">
    <PageHeader className="px-0 pt-0 pb-4" title="观察历史" right={<button className={eveningControl} onClick={() => void query.refetch()}>刷新记录</button>} />
    <EveningPersonalNav />
    <div className="flex flex-wrap items-center gap-4"><label className="space-y-1 text-xs"><span className="block text-secondary">记录月份</span><input aria-label="记录月份" type="month" value={month} className={eveningControl} onChange={event => setMonth(event.target.value)} /></label><p className="max-w-2xl text-xs leading-5 text-secondary">保存每次推荐的原始引用；连续观察共享同一跟踪过程。星标只记审美，自报盈亏与推荐后价格表现分开记录。</p></div>
    {query.isLoading && <p role="status" className="py-8 text-center text-secondary">正在读取本月观察历史…</p>}
    {query.isError && <p role="alert" className="rounded-lg border border-border p-4 text-danger">历史读取失败：{query.error.message}<button className={`${eveningControl} ml-3`} onClick={() => void query.refetch()}>重试读取</button></p>}
    {query.data && query.data.user_id === user?.id && <HistoryContent key={`${user?.id}:${month}`} userId={user!.id} history={query.data} />}
  </div>
}

function HistoryContent({ userId, history }: { userId: string; history: EveningPersonalHistory }) {
  const [day, setDay] = useState('')
  const [filters, setFilters] = useState({ ...emptyEveningFilters })
  const [detailId, setDetailId] = useState<string | null>(null)
  const feedback = useEveningFeedback(userId)
  const sort = useTableSort<EveningPersonalRecord>(sortValue)
  const records = history.records.map(record => ({ ...record, observation: record.observation?.variables ? record.observation : null }))
  const themes = [...new Map(records.flatMap(record => record.observation?.primary_theme?.id ? [[record.observation.primary_theme.id, record.observation.primary_theme] as const] : [])).values()]
  const visible = sort.sortRows(records.filter(record => (!day || record.day === day) && matchesEveningFilters(record, filters, record.feedback)), columns)
  const detail = visible.find(record => record.id === detailId)
  const events = detail ? history.events.filter(event => event.episode_id === detail.episode_id) : []
  const actions: Record<string, string> = { star_set: '设置星标', star_cancel: '取消星标', profit_set: '标记盈利', profit_cancel: '取消盈利标记', loss_set: '标记亏损', loss_cancel: '取消亏损标记' }
  const renderCell = (record: EveningPersonalRecord, column: ColumnConfig) => {
    const cell = 'px-3 py-3 align-top'
    if (column.id === 'day') return <td className={`${cell} whitespace-nowrap text-xs`}><Link className="text-accent hover:underline" to={reportUrl(record)}>{record.day}</Link><span className="mt-1 block text-muted">原推荐</span></td>
    if (column.id === 'stock') return <td className={`${cell} min-w-32`} data-history-stock={record.symbol}><p className="font-semibold">{record.name}</p><p className="mt-1 font-mono text-xs text-muted">{record.symbol}</p></td>
    if (column.id === 'theme') return <td className={`${cell} min-w-40`}><p className="text-xs">{record.observation?.primary_theme?.name ?? '题材旧版未记录'}</p><p className="mt-1 text-xs text-secondary">{roleLabels[record.observation?.role ?? ''] ?? '角色旧版未记录'}{record.observation?.role_status === 'review_pending' && ' · 待复核'}</p></td>
    if (column.id === 'detail') return <td className={`${cell} min-w-36 text-xs`}><p className="text-secondary">{record.ended_at ? `已结束 ${record.ended_at}` : '持续观察'}</p><button className="min-h-9 text-accent hover:underline" aria-expanded={detailId === record.id} onClick={() => setDetailId(detailId === record.id ? null : record.id)}>{detailId === record.id ? '收起记录' : '过程与反馈变更'}</button></td>
    if (column.id === 'feedback') return <td className={`${cell} sticky right-0 z-[1] min-w-[156px] bg-surface`}><EveningFeedbackButtons name={record.name} record={record} state={feedback.states[record.episode_id]} onChange={feedback.change} onRetry={() => feedback.retry(record.episode_id)} /></td>
    const performance = record.performance[column.id as '1' | '3' | '5']
    return <td className={`${cell} text-right text-xs`}><p className={`whitespace-nowrap font-mono ${priceColorClass(performance?.return)}`}>{performance?.status === 'evaluated' ? fmtPct(performance.return) : performance?.status === 'not_due' ? '未到期' : '不可评价'}</p><p className="mt-1 max-w-44 text-[11px] text-muted">{performance?.reason ?? performance?.target_date}</p></td>
  }
  return <div className="space-y-4">
    <EveningFilters value={filters} onChange={setFilters} onClear={() => { setDay(''); setFilters({ ...emptyEveningFilters }) }} themes={themes} variant="history" />
    <div className="flex flex-wrap items-center justify-between gap-3 text-xs"><label className="flex items-center gap-2 text-secondary">推荐日期<select aria-label="推荐日期" className={eveningControl} value={day} onChange={event => setDay(event.target.value)}><option value="">本月全部</option>{[...new Set(records.map(record => record.day))].map(value => <option key={value}>{value}</option>)}</select></label><span role="status" className="text-secondary">当前显示 {visible.length} / 本月 {records.length} 条推荐</span><button disabled={!sort.sort} className={eveningControl} onClick={sort.reset}>恢复原始顺序</button></div>
    {!records.length ? <div className="rounded-lg border border-border py-10 text-center"><p>本月暂无观察记录</p><p className="mt-2 text-xs text-secondary">正常发布的推荐会自动记录，不需要先设置星标或盈亏。</p></div> : !visible.length ? <p className="rounded-lg border border-border py-10 text-center text-sm">没有符合当前筛选的观察记录</p> : <StockDataTable columns={columns} rows={visible} rowKey={record => record.id} renderCell={renderCell} sort={sort.sort} onSortToggle={sort.toggle} sortableColumnIds={sortable} minWidth={1140} className="overflow-x-auto rounded-lg border border-border bg-surface" />}
    {detail && <section className="space-y-4 rounded-lg border border-border bg-surface p-4 sm:p-5" aria-label={`${detail.name}跟踪过程`}>
      <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="font-semibold">{detail.name} · {detail.day}</h2><p className="mt-1 text-xs text-secondary">过程开始 {detail.started_at} · 最近观察 {detail.last_seen}{detail.ended_at ? ` · 结束 ${detail.ended_at}` : ''}</p></div><button className={eveningControl} onClick={() => setDetailId(null)}>关闭记录</button></div>
      <Link to={reportUrl(detail)} className="inline-block text-sm text-accent hover:underline">查看当时报告与原始顺序 →</Link>
      <EveningObservationDetails observation={detail.observation} />
      <EveningPerformanceDetails record={detail} />
      <div><h3 className="text-sm font-medium">本月同一过程的推荐</h3><div className="mt-2 flex flex-wrap gap-3 text-xs">{records.filter(record => record.episode_id === detail.episode_id).map(record => <Link key={record.id} to={reportUrl(record)} className="text-accent hover:underline">{record.day}{record.id === record.first_reference ? ' · 首次推荐锚点' : ''}</Link>)}</div></div>
      <div><h3 className="text-sm font-medium">此过程的反馈变更</h3>{events.length ? <ol className="mt-2 space-y-2 text-xs text-secondary">{events.map(event => <li key={event.request_id}><time className="mr-3 font-mono">{cnDateTimeFromUtc(event.at) || event.at}</time>{event.actions.map(action => actions[action] ?? action).join('；')}</li>)}</ol> : <p className="mt-2 text-xs text-muted">尚无设置或取消记录，未标记不表示负面反馈。</p>}</div>
    </section>}
  </div>
}
