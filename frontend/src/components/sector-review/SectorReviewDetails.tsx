import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, ApiError, type SectorReviewMode, type SectorReviewParams, type SectorReviewRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { amount, CLASSIFICATIONS, OBSERVATION_STATES, pct, pp, ratio, REASONS, reasonCounts, truth } from './format'

export function SectorReviewDetails({ row, mode, params, snapshotId, onRefresh }: {
  row: SectorReviewRow; mode: SectorReviewMode; params: SectorReviewParams; snapshotId: string; onRefresh: () => void
}) {
  const [showMembers, setShowMembers] = useState(false)
  const [page, setPage] = useState(1)
  const [preview, setPreview] = useState<{ symbol: string; name?: string } | null>(null)
  const members = useQuery({
    queryKey: QK.sectorReviewMembers(mode, params, row.sector_id, snapshotId, page),
    queryFn: ({ signal }) => api.sectorReviewMembers(mode, params, row.sector_id, snapshotId, page, signal),
    enabled: showMembers, staleTime: 300_000, retry: false,
  })
  const conflict = members.error instanceof ApiError && members.error.status === 409
  return <div className="space-y-4 rounded-lg border border-border bg-surface p-4 text-xs text-secondary" aria-label={`${row.name}历史证据`}>
    <div className="flex flex-wrap justify-between gap-2"><strong className="text-foreground">{row.path} · 历史证据</strong><span>纳入 {row.formed_members}/{row.original_members} · {row.eligible_for_comparison ? '达到比较成员下限' : '少于5名形成成员，未进入默认比较'}</span></div>
    <p>形成时排除：{reasonCounts(row.exclusions)}。观察期缺失：{reasonCounts(row.missing_reasons)}。</p>
    <div className="overflow-x-auto">
      <table className="w-full min-w-[940px] text-left tabular-nums">
        <thead className="text-muted"><tr>{['交易日', '板块 / 基准', '相对净值收益', mode === 'event' ? '较事件前20日' : '较此前20日', '上涨广度 / 广度差', '上涨且扩额广度', '成交额 / 份额变化', '日状态 / 覆盖', '条件证据'].map(h => <th key={h} className="pb-2 pr-4 font-normal">{h}</th>)}</tr></thead>
        <tbody>{row.observations.map((o, index) => <tr key={o.date ?? index} className="border-t border-border align-top">
          <td className="py-2 pr-3 whitespace-nowrap">{mode === 'event' ? `T${index ? `+${index}` : ''} · ` : ''}{o.date ?? '日历未知'}</td>
          <td className="py-2 pr-3">{pct(o.day_return)} / {pct(o.benchmark_return)}</td><td>{pct(o.relative_return)}</td><td>{ratio(o.amount_ratio)}</td>
          <td>{pct(o.breadth_up)} / {pp(o.breadth_diff)}</td><td>{pct(o.breadth_expanding)}</td><td>{amount(o.amount)} / {pp(o.share_delta)}</td>
          <td>{OBSERVATION_STATES[o.status] ?? o.status}<br />{o.members != null ? `收益 ${o.return_count}/${o.members} · 额 ${o.amount_count}/${o.members}` : '—'}{o.daily_classification && <><br />{CLASSIFICATIONS[o.daily_classification]}</>}</td>
          <td>{['ready', 'partial', 'data_missing'].includes(o.status) ? <span>价格走强：{truth(o.strength)}<br />同日上涨：{truth(o.same_day_up)}<br />双额扩张：{truth(o.amount_expansion)}<br />过半上涨：{truth(o.majority_up)}<br />共振候选：{truth(o.daily_resonance_candidate)}</span> : '—'}</td>
        </tr>)}</tbody>
      </table>
    </div>
    {mode === 'event' && <div className="grid gap-2 sm:grid-cols-2">{row.observations.filter(o => o.date && ['ready', 'partial', 'data_missing'].includes(o.status)).map(o => <p key={o.date}>{o.date} · 较上一观察日：相对收益 {pp(o.relative_return_delta)}，广度 {pp(o.breadth_up_delta)}，额比 {o.amount_ratio_delta == null ? '—' : o.amount_ratio_delta.toFixed(2)}。成交份额 {pct(o.amount_share)}；核心上涨 {o.core_up_count ?? '—'}/{o.core_count ?? '—'}，核心成交占比 {pct(o.core_amount_share)}。</p>)}</div>}
    {row.background && <div className="space-y-2"><h3 className="font-medium text-foreground">事前背景（截至T−1，同一固定成员）</h3>{row.background_label && <p className="text-accent">{row.background_label}</p>}<div className="grid gap-2 sm:grid-cols-3">{Object.entries(row.background).map(([w, bg]) => <div key={w} className="rounded border border-border p-3">{w}个交易日 · {CLASSIFICATIONS[bg.classification]}<p className="mt-1">板块 {pct(bg.sector_return)} · 基准 {pct(bg.benchmark_return)} · 相对 {pct(bg.relative_return)}</p><p className="mt-1">下跌日跑赢 {bg.down_days.wins}/{bg.down_days.comparable_days} · 指数下跌共 {bg.down_days.benchmark_down_days ?? '未知'} 天</p></div>)}</div></div>}
    <div><h3 className="mb-2 font-medium text-foreground">事前成交额核心（形成日前20日均额前5，名单固定）</h3><div className="flex flex-wrap gap-2">{row.core_members.map(member => <div className="rounded border border-border p-2" key={member.symbol}><strong>{member.name ?? member.symbol}</strong><p>事前均额 {amount(member.formation_mean_amount)}</p>{member.observations.map(o => <p key={o.date}>{o.date} · {pct(o.day_return)} · {ratio(o.amount_ratio)}</p>)}</div>)}{!row.core_members.length && <p>形成成员不足，尚无可用核心证据。</p>}</div></div>
    <button className="rounded border border-border px-3 py-2 text-accent" onClick={() => setShowMembers(!showMembers)}>{showMembers ? '收起成员证据' : '查看历史成员证据'}</button>
    {showMembers && <div className="space-y-3">
      {members.isLoading && <p role="status">正在读取历史成员…</p>}
      {members.isError && <div role="alert"><p>{conflict ? '数据已更新，刷新后查看' : `历史成员读取失败：${members.error.message}`}</p><button className="mt-2 text-accent" onClick={() => conflict ? onRefresh() : members.refetch()}>{conflict ? '刷新复盘' : '重试成员查询'}</button></div>}
      {!members.isError && members.data && <>
        <p>对应复盘日期的正式数据，未合并当前报价。共 {members.data.total} 名成员。</p>
        <div className="overflow-x-auto"><table className="w-full min-w-[700px] text-left"><thead className="text-muted"><tr>{['股票 / 形成资格', '窗口收益', '每日历史证据', '当前行情入口'].map(h => <th key={h} className="pb-2 font-normal">{h}</th>)}</tr></thead><tbody>{members.data.rows.map(member => <tr key={member.symbol} className="border-t border-border align-top"><td className="py-2 pr-3">{member.name ?? member.symbol} {member.is_core && <span className="text-accent">成交核心</span>}<p className="text-muted">{member.symbol} · {member.formation_eligible ? '纳入' : REASONS[member.exclusion_reason ?? ''] ?? '未纳入'}</p></td><td>{pct(member.window_return)}</td><td>{member.observations.map(o => <p className="py-1" key={o.date}>{o.date} · 前复权收盘 {o.price?.toFixed(3) ?? '—'} · {pct(o.day_return)} · 成交额 {amount(o.amount)} · {ratio(o.amount_ratio)}{o.reason && ` · ${REASONS[o.reason] ?? o.reason}`}</p>)}</td><td><button className="text-accent" onClick={() => setPreview({ symbol: member.symbol, name: member.name ?? undefined })}>查看当前行情</button></td></tr>)}</tbody></table></div>
        <div className="flex items-center gap-4"><button disabled={page === 1} onClick={() => setPage(page - 1)} className="disabled:opacity-40">上一页</button><span>第 {page} 页</span><button disabled={page * 30 >= members.data.total} onClick={() => setPage(page + 1)} className="disabled:opacity-40">下一页</button></div>
      </>}
    </div>}
    {preview && <StockPreviewDialog symbol={preview.symbol} name={preview.name} onClose={() => setPreview(null)} />}
  </div>
}
