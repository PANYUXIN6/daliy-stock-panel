import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { eveningPerformanceQueryOptions } from '@/lib/useSharedQueries'
import { useCurrentUser } from '@/lib/auth'
import { fmtPct, priceColorClass } from '@/lib/format'
import { PageHeader } from '@/components/PageHeader'
import { EveningPersonalNav } from '@/components/evening-report/EveningPersonalNav'
import { eveningControl } from '@/components/evening-report/EveningFilters'

export function EveningFeedbackOverview() {
  const user = useCurrentUser()
  const query = useQuery({ ...eveningPerformanceQueryOptions, queryKey: QK.eveningFeedbackOverview(user?.id ?? ''),
    queryFn: ({ signal }) => api.eveningFeedbackOverview(signal), enabled: !!user })
  const data = query.data?.user_id === user?.id ? query.data : undefined
  return <div className="mx-auto min-w-0 max-w-[1600px] space-y-5 p-4 pb-8 sm:p-6 sm:pb-10 lg:p-8">
    <PageHeader className="px-0 pt-0 pb-4" title="反馈概览" right={<button className={eveningControl} onClick={() => void query.refetch()}>刷新概览</button>} />
    <EveningPersonalNav />
    <p className="max-w-3xl text-sm leading-6 text-secondary">这里分别统计审美星标、自报盈亏和推荐后价格表现。同一跟踪过程只计一次当前反馈，取消后从已标记数量移除；这些记录不改变名单与排序。</p>
    {query.isLoading && <p role="status" className="py-8 text-center text-secondary">正在读取反馈概览…</p>}
    {query.isError && <p role="alert" className="rounded-lg border border-border p-4 text-danger">概览读取失败：{query.error.message}<button className={`${eveningControl} ml-3`} onClick={() => void query.refetch()}>重试读取</button></p>}
    {data && <>
      <div className="flex flex-wrap items-baseline gap-x-6 gap-y-2 border-b border-border pb-4"><span className="text-sm text-secondary">跟踪过程 <strong className="ml-2 font-mono text-xl text-foreground">{data.episode_count}</strong></span><span className="text-sm text-secondary">推荐记录 <strong className="ml-2 font-mono text-xl text-foreground">{data.recommendation_count}</strong></span></div>
      <div className="grid gap-4 lg:grid-cols-2">
        <section className="rounded-lg border border-border bg-surface p-5" aria-label="审美反馈"><h2 className="font-semibold">审美星标</h2><dl className="mt-5 grid grid-cols-2 gap-4"><div><dt className="text-xs text-muted">已星标</dt><dd className="mt-2 font-mono text-3xl text-accent">{data.starred}</dd></div><div><dt className="text-xs text-muted">未星标</dt><dd className="mt-2 font-mono text-3xl">{data.unstarred}</dd></div></dl><p className="mt-4 text-xs text-secondary">样本：{data.episode_count} 个跟踪过程。星标仅表示符合审美，未星标不表示不喜欢。</p></section>
        <section className="rounded-lg border border-border bg-surface p-5" aria-label="自报盈亏"><h2 className="font-semibold">自报盈亏</h2><dl className="mt-5 grid grid-cols-3 gap-4"><div><dt className="text-xs text-muted">盈</dt><dd className="mt-2 font-mono text-3xl text-red-700 dark:text-red-400">{data.profit}</dd></div><div><dt className="text-xs text-muted">亏</dt><dd className="mt-2 font-mono text-3xl text-green-700 dark:text-green-400">{data.loss}</dd></div><div><dt className="text-xs text-muted">未标记</dt><dd className="mt-2 font-mono text-3xl">{data.unmarked}</dd></div></dl><p className="mt-4 text-xs text-secondary">已标记 {data.profit + data.loss} / {data.episode_count} 个过程。不推断持仓、交易次数或未标记结果。</p></section>
      </div>
      <section className="space-y-4 rounded-lg border border-border bg-surface p-4 sm:p-5" aria-label="客观观察表现"><div><h2 className="font-semibold">推荐后价格表现</h2><p className="mt-2 text-xs leading-5 text-secondary">前向汇总采用跟踪过程首次正式推荐的收盘价，只计交易日当晚发布的定稿。各窗口分别统计可评价、未到期、待回看和不可评价样本。</p></div><div className="overflow-x-auto"><table className="w-full min-w-[620px] text-sm"><thead><tr className="border-b border-border text-left text-xs text-secondary">{['窗口', '总样本', '可评价', '未到期', '已到期，待回看', '不可评价', '平均价格变化'].map(label => <th key={label} scope="col" className="whitespace-nowrap px-3 py-3 font-medium">{label}</th>)}</tr></thead><tbody>{(['1', '3', '5'] as const).map(window => { const result = data.performance[window]; return <tr key={window} className="border-b border-border/50 last:border-0"><th scope="row" className="px-3 py-4 text-left font-medium">D+{window}</th><td className="px-3 font-mono">{result.total}</td><td className="px-3 font-mono">{result.evaluated}</td><td className="px-3 font-mono">{result.not_due}</td><td className="px-3 font-mono">{result.overdue_pending_review}</td><td className="px-3 font-mono">{result.unavailable}</td><td className={`px-3 font-mono ${priceColorClass(result.mean_return)}`}>{fmtPct(result.mean_return)}</td></tr> })}</tbody></table></div><p className="text-xs text-muted">均值仅基于该窗口可评价样本。同一跟踪过程的多个窗口共用一个起点。</p></section>
      {data.episode_count === 0 && <p className="text-center text-sm text-secondary">尚无观察过程。晚报正常生成后会自动积累记录，无需先提交反馈。</p>}
    </>}
  </div>
}
