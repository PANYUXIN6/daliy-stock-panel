import { Fragment, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { api, type SectorReviewMode, type SectorReviewParams, type SectorReviewRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { SectorReviewDetails } from '@/components/sector-review/SectorReviewDetails'
import { amount, CLASSIFICATIONS, pct, pp, ratio, reasonCounts, truth } from '@/components/sector-review/format'

const inputClass = 'min-w-0 rounded border border-border bg-elevated px-2 py-1.5 text-xs text-foreground'
function firstStrength(row: SectorReviewRow) {
  if (row.confirmed_first_strength_date) return `${row.first_strength_offset === 0 ? '当日走强' : `延后走强（T+${row.first_strength_offset}）`} · ${row.confirmed_first_strength_date}`
  if (row.earliest_observed_strength_date) return `最早已观察到 ${row.earliest_observed_strength_date}，首次不确定`
  return row.observations.filter(o => ['ready', 'partial', 'data_missing'].includes(o.status)).some(o => o.strength == null) ? '走强日期待评估' : '尚未观察到走强'
}
function streak(row: SectorReviewRow) { return row.strength_streak == null ? '—' : `${row.streak_complete ? '' : '至少'}${row.strength_streak}日` }

export function SectorReview() {
  const [search, setSearch] = useSearchParams()
  const [filter, setFilter] = useState('default')
  const [onlyIncomplete, setOnlyIncomplete] = useState(false)
  const [nameFilter, setNameFilter] = useState('')
  const [sort, setSort] = useState('relative')
  const [expanded, setExpanded] = useState<{ context: string; sector: string } | null>(null)
  const availability = useQuery({ queryKey: QK.sectorReviewAvailability, queryFn: ({ signal }) => api.sectorReviewAvailability(signal), staleTime: 30_000 })
  const info = availability.data
  const mode: SectorReviewMode = search.get('view') === 'countertrend' ? 'countertrend' : 'event'
  const kind = search.get('kind') === 'concept' ? 'concept' : 'industry'
  const dimensions = info?.dimensions.filter(d => d.kind === kind) ?? []
  const dimension = search.get('dimension_id') ?? dimensions[0]?.dimension_id ?? ''
  const benchmark = search.get('benchmark') ?? '000001.SH'
  const sourceLevels = dimensions.find(d => d.dimension_id === dimension)?.levels ?? [1, 2, 3]
  const requestedLevel = Number(search.get('level') ?? 1)
  const level = kind === 'concept' ? 1 : sourceLevels.includes(requestedLevel) ? requestedLevel : sourceLevels[0] ?? 1
  const window = Number(search.get('window') ?? 20)
  const day = search.get(mode === 'event' ? 'event_date' : 'as_of') ?? info?.default_date ?? info?.latest_local_date ?? ''
  const viewAsOf = search.get('view_as_of') ?? day
  const params: SectorReviewParams = { kind, dimension_id: dimension, level: kind === 'industry' ? level : 1, benchmark,
    ...(mode === 'event' ? { event_date: day, view_as_of: viewAsOf } : { as_of: day, window }) }
  const contextKey = JSON.stringify([mode, params])
  useEffect(() => {
    if (!info || !day || !dimension) return
    const next = new URLSearchParams(search)
    const defaults: Record<string, string> = { view: mode, kind, dimension_id: dimension, benchmark, level: String(level), window: String(window), [mode === 'event' ? 'event_date' : 'as_of']: day }
    if (mode === 'event') defaults.view_as_of = viewAsOf
    let changed = false
    for (const [key, value] of Object.entries(defaults)) {
      if (!next.has(key)) { next.set(key, value); changed = true }
      else if (key === 'level' && next.get(key) !== value) { next.set(key, value); changed = true }
    }
    if (changed) setSearch(next, { replace: true })
  }, [info, day, dimension, mode, kind, benchmark, level, window, viewAsOf, search, setSearch])
  const query = useQuery({ queryKey: QK.sectorReview(mode, params), queryFn: ({ signal }) => api.sectorReview(mode, params, signal),
    enabled: !!day && !!dimension && !!info, staleTime: 300_000, retry: false })
  const data = query.data
  const filtering = filter !== 'default' || onlyIncomplete || nameFilter.length > 0
  const update = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(search)
    // Persist the effective context the first time controls are used.
    Object.entries({ view: mode, kind, dimension_id: dimension, benchmark, level: String(level), window: String(window), [mode === 'event' ? 'event_date' : 'as_of']: day }).forEach(([k, v]) => { if (v) next.set(k, v) })
    if (mode === 'event' && viewAsOf) next.set('view_as_of', viewAsOf)
    Object.entries(patch).forEach(([k, v]) => v == null ? next.delete(k) : next.set(k, v))
    const nextKind = next.get('kind')
    const nextSource = info?.dimensions.find(d => d.kind === nextKind && d.dimension_id === next.get('dimension_id'))
    if (nextKind === 'concept') next.set('level', '1')
    else if (nextSource && !nextSource.levels.includes(Number(next.get('level')))) next.set('level', String(nextSource.levels[0] ?? 1))
    setSearch(next, { replace: true }); setExpanded(null)
  }
  const changeMode = (next: SectorReviewMode) => { setFilter('default'); update({ view: next }) }
  const refresh = async () => { await availability.refetch(); await query.refetch(); setExpanded(null) }
  const eventIndex = info?.calendar?.days.indexOf(day) ?? -1
  const followup = eventIndex >= 0 ? info!.calendar!.days.slice(eventIndex, eventIndex + 4) : []
  const latestObservation = followup.filter(d => d <= (info?.default_date ?? '')).at(-1)
  const rows = (data?.rows ?? []).filter(row => {
    const match = mode === 'event' ? filter === 'all' || (row.eligible_for_comparison && (filter === 'false' ? row.candidate === false : filter === 'unknown' ? row.candidate == null : row.candidate === true))
      : filter === 'all' || (row.eligible_for_comparison && (filter === 'default' ? ['countertrend_up', 'resilient'].includes(row.classification) : row.classification === filter))
    return match && (!onlyIncomplete || !row.data_complete) && (!nameFilter || row.path.includes(nameFilter))
  }).sort((a, b) => {
    if (sort === 'first') return (a.confirmed_first_strength_date ?? '9999').localeCompare(b.confirmed_first_strength_date ?? '9999') || a.sector_id.localeCompare(b.sector_id)
    const av = sort === 'amount' ? a.latest.amount_ratio : a.relative_return
    const bv = sort === 'amount' ? b.latest.amount_ratio : b.relative_return
    return av == null ? bv == null ? a.sector_id.localeCompare(b.sector_id) : 1 : bv == null ? -1 : bv - av || a.sector_id.localeCompare(b.sector_id)
  })
  const toggle = (row: SectorReviewRow) => setExpanded(expanded?.context === contextKey && expanded.sector === row.sector_id ? null : { context: contextKey, sector: row.sector_id })
  const details = (row: SectorReviewRow) => data?.snapshot_id && <SectorReviewDetails key={`${contextKey}:${data.snapshot_id}:${row.sector_id}`} row={row} mode={mode} params={params} snapshotId={data.snapshot_id} onRefresh={refresh} />
  return <div className="flex h-full min-h-0 flex-col">
    <PageHeader title="板块复盘" subtitle="日线量价观察 · 事件共振跟踪与调整逆势" right={<button type="button" aria-label="刷新板块复盘" className="rounded p-2 text-muted hover:bg-elevated disabled:opacity-40" disabled={query.isFetching || availability.isFetching} onClick={refresh}><RefreshCw className={`h-4 w-4 ${query.isFetching ? 'animate-spin' : ''}`} /></button>} />
    <div className="flex-1 min-h-0 overflow-y-auto p-4 md:p-6 space-y-4">
      <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-surface p-3 text-xs">
        <label className="flex items-center gap-2">分类<select className={inputClass} aria-label="板块分类" value={kind} onChange={e => { setFilter('default'); update({ kind: e.target.value, dimension_id: null, level: '1' }) }}><option value="industry">行业</option><option value="concept">概念</option></select></label>
        {dimensions.length > 1 ? <label className="flex items-center gap-2">来源<select className={inputClass} aria-label="分类来源" value={dimension} onChange={e => update({ dimension_id: e.target.value })}>{dimensions.map(d => <option value={d.dimension_id} key={d.dimension_id}>{d.label}</option>)}</select></label> : <span className="text-muted">{dimensions[0]?.label ?? '该分类尚无可用来源'}</span>}
        {kind === 'industry' && <label className="flex items-center gap-2">层级<select className={inputClass} aria-label="行业层级" value={level} onChange={e => update({ level: e.target.value })}>{(dimensions.find(d => d.dimension_id === dimension)?.levels ?? [1, 2, 3]).map(n => <option value={n} key={n}>{['', '一级', '二级', '三级'][n]}</option>)}</select></label>}
        <label className="flex items-center gap-2">基准<select className={inputClass} aria-label="复盘基准" value={benchmark} onChange={e => update({ benchmark: e.target.value })}>{(info?.benchmarks ?? [{ symbol: '000001.SH', name: '上证指数' }]).map(b => <option value={b.symbol} key={b.symbol}>{b.name}</option>)}</select></label>
        <label className="flex items-center gap-2">{mode === 'event' ? '事件日T' : '截至日D'}<input className={inputClass} aria-label={mode === 'event' ? '事件日T' : '逆势截至日'} type="date" value={day} min={info?.calendar?.start} max={info?.calendar?.end} onChange={e => update(mode === 'event' ? { event_date: e.target.value, view_as_of: e.target.value } : { as_of: e.target.value })} /></label>
        <button className="text-accent disabled:text-muted" disabled={!info?.default_date} onClick={() => update(mode === 'event' ? { event_date: info?.default_date ?? '', view_as_of: info?.default_date ?? '' } : { as_of: info?.default_date ?? '' })}>返回最近完整日</button>
      </div>
      <div className="rounded-lg border border-border bg-surface p-3 text-xs leading-6" aria-label="数据边界">
        <p className="font-medium text-warning">当前分类名册回看历史行情</p>
        <p>请求日期 {day || '未选定'}{mode === 'event' && ` · 观察截至 ${viewAsOf || '未选定'}`} · 实际计算截至 {data?.context.as_of ?? '—'} · 形成日 {data?.context.formation_date ?? '—'}</p>
        <p>已评估 {data?.coverage.evaluated_sectors ?? '—'}/{data?.coverage.total_sectors ?? '—'} 个板块 · 完整证据 {data?.coverage.complete_sectors ?? '—'} · 可比较A股样本 {data?.coverage.formed_market_members ?? '—'}/{data?.coverage.original_market_members ?? '—'} · 交易所 {data?.coverage.exchanges?.join('、') ?? '—'}</p>
        {info?.warnings.map(w => <p key={w} className="text-warning">{w}</p>)}{data?.warnings.map(w => <p key={w} className="text-muted">{w}</p>)}
        {data?.coverage.market_exclusions && <p className="text-muted">市场样本形成时排除：{reasonCounts(data.coverage.market_exclusions)}</p>}
        {query.isFetching && data && <p role="status" className="text-accent">正在刷新，暂保留本次上下文的上一份结果…</p>}
      </div>
      <div className="flex flex-wrap gap-2" role="tablist" aria-label="复盘视图">{([['event', '事件共振跟踪'], ['countertrend', '调整逆势']] as const).map(([value, label]) => <button key={value} role="tab" aria-selected={mode === value} className={`rounded-lg px-4 py-2 text-sm ${mode === value ? 'bg-accent text-white' : 'bg-surface text-secondary border border-border'}`} onClick={() => changeMode(value)}>{label}</button>)}</div>
      <div className="flex flex-wrap items-center gap-3 text-xs">
        {mode === 'event' ? <><label className="flex items-center gap-2">观察截至S<input className={inputClass} aria-label="观察截至日S" type="date" value={viewAsOf} min={day} max={followup.at(-1) ?? info?.calendar?.end} onChange={e => update({ view_as_of: e.target.value })} /></label><button className="text-accent disabled:text-muted" disabled={!latestObservation} onClick={() => update({ view_as_of: latestObservation ?? day })}>观察到T+3或最近完整日</button>{!data?.response_overview?.window_finished && <span className="text-muted">观察窗口未结束</span>}</> : <label className="flex items-center gap-2">窗口<select className={inputClass} aria-label="逆势窗口" value={window} onChange={e => update({ window: e.target.value })}>{[5, 10, 20, 60].map(n => <option value={n} key={n}>{n}个交易日</option>)}</select></label>}
      </div>
      {data?.benchmark_evidence && <section className="rounded-lg border border-border bg-surface p-4 text-xs space-y-3">
        <h2 className="font-medium text-foreground">{mode === 'event' ? '事件日指数证据' : '截至日指数证据'} · {data.context.benchmark_name}</h2>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">{[['当日收益', pct(data.benchmark_evidence.day_return)], ['事前5 / 20日收益', `${pct(data.benchmark_evidence.pre_returns['5'])} / ${pct(data.benchmark_evidence.pre_returns['20'])}`], ['盘中最低相对昨收 / 收盘位置', `${pct(data.benchmark_evidence.low_vs_prev_close)} / ${pct(data.benchmark_evidence.close_position)}`], ['较此前20日成交额 / 前20日收盘高点', `${ratio(data.benchmark_evidence.amount_ratio)} / ${data.benchmark_evidence.prior20_close_high?.toFixed(2) ?? '—'}`]].map(([label, value]) => <div key={label}><p className="text-muted">{label}</p><p className="mt-1 font-mono text-foreground">{value}</p></div>)}</div>
        {mode === 'event' && <p className="leading-6">下探收复昨收且扩额：{truth(data.benchmark_evidence.labels.reclaim_with_expansion)} · 回落后上涨且扩额：{truth(data.benchmark_evidence.labels.rebound_with_expansion)} · 突破此前20日收盘高点且扩额：{truth(data.benchmark_evidence.labels.breakout20_with_expansion)}</p>}
        <p className="text-muted">宽基ETF旁证：{data.etf?.reason ?? '尚未提供'}</p>
        {data.response_overview && <p className="border-t border-border pt-3 leading-6">T日共振候选 {data.response_overview.same_day_candidates} 个 · 各日确定首次走强：{Object.entries(data.response_overview.first_strength_counts).map(([d, n]) => `${d} ${n}个`).join('；')}<br /><span className="text-muted">{data.response_overview.overlap_note}</span></p>}
      </section>}
      <div className="flex flex-wrap items-center gap-3 text-xs">
        <label className="flex items-center gap-2">{mode === 'event' ? '候选' : '区间状态'}<select aria-label="结果筛选" className={inputClass} value={filter} onChange={e => setFilter(e.target.value)}>{mode === 'event' ? <><option value="default">已入选</option><option value="false">未入选</option><option value="unknown">待判定</option></> : <><option value="default">逆势上涨与相对抗跌</option>{Object.entries(CLASSIFICATIONS).map(([v, label]) => <option value={v} key={v}>{label}</option>)}</>}<option value="all">全部（含未达比较下限）</option></select></label>
        <label className="flex items-center gap-1"><input type="checkbox" checked={onlyIncomplete} onChange={e => setOnlyIncomplete(e.target.checked)} />仅数据不足</label>
        <input className={inputClass} aria-label="搜索板块" placeholder="搜索板块" value={nameFilter} onChange={e => setNameFilter(e.target.value)} />
        <label className="flex items-center gap-2">排序<select className={inputClass} aria-label="结果排序" value={sort} onChange={e => setSort(e.target.value)}><option value="relative">累计相对净值收益</option><option value="amount">成交额比</option>{mode === 'event' && <option value="first">首次走强日期</option>}</select></label><span className="text-muted">展示 {rows.length}/{data?.rows.length ?? 0} · 排序仅供浏览</span>
      </div>
      {availability.isLoading || query.isLoading ? <p role="status" className="py-10 text-center text-muted">正在读取本地复盘数据…</p> : null}
      {(availability.isError || query.isError) && <div role="alert" className="rounded border border-danger/30 p-4 text-sm">板块复盘加载失败：{availability.error?.message ?? query.error?.message}<button className="ml-3 text-accent" onClick={refresh}>重试</button></div>}
      {!availability.isLoading && !query.isLoading && !query.isError && !availability.isError && (!day || !dimension || data?.status === 'unavailable') && <p role="status" className="rounded border border-border p-5 text-sm text-warning">本地正式数据不足，当前无法完成比较。可选择其他交易日，或联系管理员在数据页维护正式日线、分类和日历。</p>}
      {data && !query.isLoading && rows.length === 0 && data.status !== 'unavailable' && <p className="py-10 text-center text-sm text-muted">{filtering ? '当前筛选条件下没有板块，请调整筛选。' : '该日期没有满足当前条件的板块；可切换全部核对未入选及待判定证据。'}</p>}
      <div className="hidden md:block overflow-x-auto rounded-lg border border-border">
        {rows.length > 0 && <><p className="px-3 py-2 text-[11px] text-muted">表格可横向滚动，点击板块展开历史证据。</p><table className="w-full min-w-[1140px] text-left text-xs tabular-nums"><thead className="bg-surface text-muted"><tr>{['板块 / 区间状态', mode === 'event' ? '首次走强 / T日共振' : '板块 / 基准区间收益', '累计相对净值收益', mode === 'event' ? '事前20日相对收益' : '下跌日跑赢 / 指数下跌日', mode === 'event' ? '连续走强 / 广度差' : '下跌日平均超额', '窗口上涨成员 / 中位收益', mode === 'event' ? '成交额份额变化' : '截至日成交额', '覆盖'].map(h => <th className="px-3 py-3 font-normal first:sticky first:left-0 first:bg-surface" key={h}>{h}</th>)}</tr></thead><tbody>{rows.map(row => <Fragment key={row.sector_id}><tr className="border-t border-border align-top hover:bg-surface/60"><td className="sticky left-0 z-[1] w-44 bg-surface px-3 py-3"><button className="text-accent text-left font-medium" aria-expanded={expanded?.context === contextKey && expanded.sector === row.sector_id} onClick={() => toggle(row)}>{row.name}</button><p className="mt-1 text-muted">{CLASSIFICATIONS[row.classification]}</p>{mode === 'countertrend' && <button className="mt-2 text-[11px] text-accent" onClick={() => changeMode('event')}>去事件视图（另选T）</button>}</td>
          <td className="px-3 py-3">{mode === 'event' ? <>{firstStrength(row)}<p className="mt-1 text-muted">T日共振：{truth(row.observations[0]?.daily_resonance_candidate)}</p></> : <>{pct(row.sector_return)} / {pct(row.benchmark_return)}</>}</td><td className="px-3 py-3">{pct(row.relative_return)}{mode === 'event' && <p className="mt-1 text-muted">板块 {pct(row.sector_return)}<br />基准 {pct(row.benchmark_return)}</p>}</td>
          <td className="px-3 py-3">{mode === 'event' ? pct(row.background?.['20'].relative_return) : <>{row.down_days.wins}/{row.down_days.comparable_days} · 共 {row.down_days.benchmark_down_days ?? '未知'} 天<p className="mt-1 text-muted">{row.down_days.complete ? '下跌日统计完整' : '可比较子样本'}{row.down_days.small_sample ? ' · 样本少' : ''}</p></>}</td><td className="px-3 py-3">{mode === 'event' ? <>{streak(row)}<br />{pp(row.latest.breadth_diff)}</> : pp(row.down_days.mean_excess)}</td><td className="px-3 py-3">{pct(row.member_up_ratio)} / {pct(row.member_median_return)}</td><td className="px-3 py-3">{mode === 'event' ? pp(row.latest.share_delta) : amount(row.latest.amount)}</td><td className="px-3 py-3">形成 {row.formed_members}/{row.original_members}<p className="mt-1 text-muted">{row.data_complete ? '证据完整' : '数据不足'}{!row.eligible_for_comparison ? ' · 未达下限' : ''}</p></td></tr>{expanded?.context === contextKey && expanded.sector === row.sector_id && <tr><td colSpan={8} className="p-3">{details(row)}</td></tr>}</Fragment>)}</tbody></table></>}
      </div>
      <div className="space-y-3 md:hidden">{rows.map(row => <section className="rounded-lg border border-border bg-surface p-3 text-xs" key={row.sector_id}><button className="w-full text-left" aria-expanded={expanded?.context === contextKey && expanded.sector === row.sector_id} onClick={() => toggle(row)}><div className="flex justify-between gap-2"><strong className="text-accent">{row.name}</strong><span>{CLASSIFICATIONS[row.classification]}</span></div><p className="mt-2">板块 {pct(row.sector_return)} · 基准 {pct(row.benchmark_return)} · 相对 {pct(row.relative_return)}</p><p className="mt-2 text-muted">{mode === 'event' ? firstStrength(row) : `下跌日跑赢 ${row.down_days.wins}/${row.down_days.comparable_days} · 指数下跌共 ${row.down_days.benchmark_down_days ?? '未知'} 天`}</p><p className="mt-2 text-muted">形成成员 {row.formed_members}/{row.original_members} · {row.data_complete ? '证据完整' : '数据不足'} · 点击展开</p></button>{expanded?.context === contextKey && expanded.sector === row.sector_id && <div className="mt-3">{details(row)}</div>}</section>)}</div>
      <details className="rounded-lg border border-border p-4 text-xs leading-6 text-muted"><summary className="cursor-pointer text-secondary">计算口径与版本</summary><p className="mt-2">规则 {data?.rules_version ?? 'sector-review-v2'}。成员在事件T−1或逆势窗口前一交易日形成，需21个连续正式收盘价及20日成交额；形成后不因缺失删除成员。板块每日等权，收益复合计算。相对净值收益=(1+板块收益)/(1+基准收益)−1。</p><p>事件成交额基线固定为T前20日，逆势基线为各评价日前20日；不含评价当日。下跌日统计只以同窗口指数下跌日为条件分母。“成交额扩张”不等于净流入。三值条件：满足／不满足／未知；数据完整性独立列示。</p><p>历史结果按当前数据重新计算，分类和复权修订可能改变结果。成员详情为历史证据，“查看当前行情”为独立入口。当前规则用于描述，不代表预测、交易胜率或仓位建议。</p><p>分类观察时间 {data?.membership_observed_at ?? '—'} · 分类版本 {data?.membership_fingerprint ?? '—'} · 输入快照 {data?.snapshot_id ?? '—'} · 计算时间 {data?.computed_at ?? '—'}</p></details>
    </div>
  </div>
}
