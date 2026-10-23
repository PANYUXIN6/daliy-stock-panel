import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { RefreshCw, TrendingUp } from 'lucide-react'
import { api, type StrengthLadderStock } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cnDateFromUtc, fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { toNavItems } from '@/lib/listNav'
import { BOARDS, getBoardType } from '@/lib/board'
import { boardTag } from '@/components/stock-table/primitives'
import { DatePicker } from '@/components/DatePicker'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'

const STATUS_LABELS = { high: '近60日新高', volume: '放量上涨', momentum: '动量走强' } as const

export function StrengthLadder({ asOf, onDateChange }: { asOf: string; onDateChange: (value: string) => void }) {
  const [status, setStatus] = useState<'all' | StrengthLadderStock['status']>('all')
  const [board, setBoard] = useState('all')
  const [preview, setPreview] = useState<{ symbol: string; name?: string } | null>(null)
  const query = useQuery({
    queryKey: QK.strengthLadder(asOf || undefined),
    queryFn: () => api.strengthLadder(asOf || undefined),
    staleTime: 30_000,
  })
  const tiers = useMemo(() => (query.data?.tiers ?? []).map(tier => ({
    ...tier,
    stocks: tier.stocks.filter(stock => (status === 'all' || stock.status === status)
      && (board === 'all' || getBoardType(stock.symbol) === board)),
  })).filter(tier => tier.stocks.length > 0), [query.data, status, board])
  const navList = useMemo(() => toNavItems(tiers.flatMap(tier => tier.stocks)), [tiers])
  const shown = navList.length
  const totalRanked = query.data?.tiers.reduce((sum, tier) => sum + tier.count, 0) ?? 0

  return <div className="flex h-full min-h-0 flex-col">
    <PageHeader title="趋势强度梯队" className="flex-wrap" subtitle="按20个交易日涨幅分档，观察持续走强的股票" right={
      <button type="button" onClick={() => query.refetch()} disabled={query.isFetching} aria-label="刷新趋势梯队" className="rounded p-1.5 text-muted hover:bg-surface disabled:opacity-50">
        <RefreshCw className={`h-4 w-4 ${query.isFetching ? 'animate-spin' : ''}`} />
      </button>
    } />
    <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-2 text-xs">
      <DatePicker value={asOf || query.data?.as_of || cnDateFromUtc(new Date().toISOString())} onChange={onDateChange} />
      {asOf && <button type="button" className="text-accent" onClick={() => onDateChange('')}>返回最新</button>}
      <label className="flex items-center gap-1 text-secondary">状态
        <select aria-label="趋势状态" value={status} onChange={event => setStatus(event.target.value as typeof status)} className="rounded border border-border bg-elevated px-2 py-1">
          <option value="all">全部</option>
          {Object.entries(STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
      <label className="flex items-center gap-1 text-secondary">板块
        <select aria-label="趋势板块" value={board} onChange={event => setBoard(event.target.value)} className="rounded border border-border bg-elevated px-2 py-1">
          <option value="all">全部</option>
          {BOARDS.map(value => <option key={value} value={value}>{value}</option>)}
        </select>
      </label>
      {query.data && !query.isError && <span className="text-muted">展示 {shown} / 入榜 {totalRanked} · 可计算 {query.data.eligible}/{query.data.total}</span>}
    </div>
    <div className="border-b border-border px-4 py-2 text-[11px] text-muted space-y-1">
      <p>20日涨幅至少3%入榜。状态优先级：近60日新高 → 放量上涨 → 动量走强；新高容差0.5%，放量要求5日量比≥1.5且当日上涨。</p>
      {query.data && !query.isError && <p>全市场近60日新高 {query.data.counts.near_high} 只 / 近60日新低 {query.data.counts.near_low} 只（含0.5%容差）。价格与动量使用前复权口径。</p>}
      {!query.isError && query.data?.warnings.map(warning => <p key={warning} className="text-warning">{warning}</p>)}
    </div>
    {query.isLoading ? <div role="status" className="flex flex-1 items-center justify-center text-muted"><RefreshCw className="mr-2 h-4 w-4 animate-spin" />加载趋势梯队…</div>
      : query.isError ? <ErrorState title="趋势梯队加载失败" retrying={query.isFetching} onRetry={() => query.refetch()} />
      : tiers.length === 0 ? <EmptyState icon={TrendingUp} title="暂无趋势股票" hint={totalRanked ? '当前筛选条件下没有股票，请调整状态或板块' : '该日期没有达到20日涨幅3%的股票，或本地历史行情不足'} />
      : <div className="min-h-0 flex-1 overflow-y-auto p-4 space-y-3">
        {tiers.map(tier => <section key={tier.level} className="rounded-lg border border-border bg-surface/50">
          <h2 className="flex items-center gap-2 border-b border-border px-3 py-2 text-sm font-semibold">
            <TrendingUp className="h-4 w-4 text-accent" />
            20日涨幅 {Math.round(tier.minimum * 100)}%{tier.maximum == null ? '及以上' : `～${Math.round(tier.maximum * 100)}%（不含）`}
            <span className="text-xs font-normal text-muted">{tier.stocks.length} 只</span>
          </h2>
          <div className="grid grid-cols-[repeat(auto-fill,minmax(205px,1fr))] gap-2 p-3">
            {tier.stocks.map(stock => {
              const tag = boardTag(stock.symbol)
              return <button type="button" key={stock.symbol} onClick={() => setPreview({ symbol: stock.symbol, name: stock.name ?? undefined })}
                className="min-w-0 rounded-md border border-border bg-elevated/40 px-3 py-2 text-left hover:border-accent/50">
                <div className="flex min-w-0 items-center gap-1.5">
                  <span className="truncate text-sm font-medium">{stock.name || stock.symbol}</span>
                  {tag && <span className={`rounded border px-1 text-[9px] ${tag.color}`}>{tag.label}</span>}
                  <span className={`ml-auto shrink-0 font-mono text-xs ${priceColorClass(stock.change_pct)}`}>{fmtPct(stock.change_pct)}</span>
                </div>
                <div className="mt-1 flex justify-between gap-2 text-[10px] text-muted"><span>{stock.symbol}</span><span>{fmtPrice(stock.close)}</span></div>
                <div className="mt-2 flex justify-between gap-2 text-xs"><span className="text-muted">20日涨幅</span><span className="font-mono text-bull">{fmtPct(stock.momentum_20d)}</span></div>
                <div className="mt-1 flex justify-between gap-2 text-[10px]"><span className="text-accent">{STATUS_LABELS[stock.status]}</span><span className="text-muted">5日量比 {stock.vol_ratio_5d?.toFixed(2) ?? '—'}</span></div>
              </button>
            })}
          </div>
        </section>)}
      </div>}
    <StockPreviewDialog symbol={preview?.symbol ?? null} name={preview?.name ?? ''} navList={navList}
      onClose={() => setPreview(null)} onNavigate={(symbol, name) => setPreview({ symbol, name })} />
  </div>
}
