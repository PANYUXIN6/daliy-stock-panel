import { useState } from 'react'
import { RefreshCw, Loader2, X, Search, FileText, Database, Clock, ChartPie } from 'lucide-react'
import { useQueryClient } from '@tanstack/react-query'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { useFinancialStatus, useFinancialExecutionStatus } from '@/lib/useFinancials'
import { StockFinancialSearch } from '@/components/financials/StockFinancialSearch'
import { StockFinancialDetail } from '@/components/financials/StockFinancialDetail'
import { ReportHistoryPanel } from '@/components/financials/ReportHistoryPanel'
import { LastStockChip } from '@/components/LastStockChip'
import { useLastStock } from '@/lib/useLastStock'
import { fmtBigNum } from '@/lib/format'

const TABLE_LABELS: Record<string, string> = {
  metrics: '核心指标',
  income: '利润表',
  balance_sheet: '资产负债表',
  cash_flow: '现金流量表',
  shares: '股本表',
}

const TABLE_ICON: Record<string, typeof FileText> = {
  metrics: Database,
  income: FileText,
  balance_sheet: FileText,
  cash_flow: FileText,
  shares: ChartPie,
}

export function Financials() {
  const qc = useQueryClient()
  const { data: status, isLoading, isFetching, error } = useFinancialStatus()
  const execution = useFinancialExecutionStatus()
  // 选中的个股(模糊搜索结果);null 时显示搜索引导
  const [selected, setSelected] = useState<{ symbol: string; name: string } | null>(null)
  const { last: lastStock, remember: rememberStock } = useLastStock('financials')
  const pick = (symbol: string, name: string) => {
    setSelected({ symbol, name })
    rememberStock(symbol, name)
  }

  const tables = status?.tables ?? {}
  const available = Object.values(tables).some(table => table.rows > 0)
  const lastSync = execution.data?.last_sync ?? {}

  return (
    <>
      <PageHeader
        title="财务分析"
        subtitle="利润表 / 资负表 / 现金流 / 关键指标 / 股本 / AI分析"
        right={
          <div className="flex items-center gap-2">
            <LastStockChip stock={lastStock} onSelect={pick} />
            <button
              type="button"
              className="inline-flex items-center gap-1.5 rounded-btn border border-border bg-elevated px-3 py-1.5 text-xs text-secondary hover:text-foreground disabled:opacity-50"
              onClick={() => void qc.invalidateQueries({ queryKey: ['financials'] })}
              disabled={isFetching}
            >
              <RefreshCw className={`h-3.5 w-3.5 ${isFetching ? 'animate-spin' : ''}`} />刷新
            </button>
          </div>
        }
      />

      <div className="px-3 sm:px-8 py-6 space-y-6 max-w-7xl">
        {error && <p role="alert" className="text-xs text-danger">{error.message}</p>}
        {execution.isError && <p role="status" className="text-xs text-warning">采集执行状态不可用，本地财务数据仍可浏览。</p>}
        {!isLoading && available && (
          <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 xl:grid-cols-5 gap-3">
            {Object.entries(TABLE_LABELS).map(([key, label]) => {
              const info = tables[key]
              const TIcon = TABLE_ICON[key] ?? Database
              const hasData = (info?.rows ?? 0) > 0
              const lsTime = lastSync[key]
              return (
                <div key={key} className={`rounded-card border p-3.5 flex flex-col ${hasData ? 'border-border bg-surface' : 'border-dashed border-border/60 bg-elevated/20'}`}>
                  <div className="flex items-center gap-1.5">
                    <TIcon className={`h-3.5 w-3.5 ${hasData ? 'text-accent' : 'text-muted'}`} />
                    <span className="text-xs font-medium text-foreground">{label}</span>
                  </div>
                  <div className="mt-2 text-xl font-semibold tabular-nums text-foreground">
                    {fmtBigNum(info?.rows ?? 0)}
                    <span className="text-[10px] text-muted ml-1 font-normal">行</span>
                  </div>
                  <div className="text-[11px] text-muted mt-0.5">{fmtBigNum(info?.symbols ?? 0)} 只标的</div>
                  <div className="mt-auto pt-2 border-t border-border/40 text-[10px] text-muted flex items-center gap-1">
                    <Clock className="h-2.5 w-2.5 shrink-0" />
                    {execution.isError ? '同步记录不可用' : lsTime ? new Date(lsTime).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : execution.isLoading ? '读取同步记录…' : '尚未采集'}
                  </div>
                </div>
              )
            })}
          </div>
        )}

        {isLoading ? (
          <div className="flex items-center justify-center py-16">
            <Loader2 className="h-5 w-5 animate-spin text-muted" />
          </div>
        ) : !available ? (
          <div className="rounded-card border border-dashed border-border bg-surface px-6 py-14 text-center">
            <Database className="mx-auto h-8 w-8 text-muted" />
            <div className="mt-3 text-sm text-secondary">暂无财务数据</div>
            <div className="mt-1 text-xs text-muted">后台尚未采集财务数据</div>
          </div>
        ) : (
          <>
            {/* 个股搜索区 */}
            <div>
              {selected ? (
                // 已选股:紧凑搜索条 + 清除按钮(便于换股)
                <div className="flex items-center gap-3">
                  <div className="flex-1 max-w-xl">
                    <StockFinancialSearch onSelect={pick} />
                  </div>
                  <button
                    onClick={() => setSelected(null)}
                    className="inline-flex items-center gap-1 px-2.5 py-1.5 text-xs text-secondary hover:text-foreground rounded-btn border border-border hover:bg-elevated transition-colors shrink-0"
                    title="清除选择"
                  >
                    <X className="h-3.5 w-3.5" />
                    清除
                  </button>
                </div>
              ) : (
                // 未选股:醒目居中引导
                <div className="flex flex-col items-center gap-3 py-8">
                  <div className="flex items-center gap-2 text-sm text-secondary">
                    <Search className="h-4 w-4 text-accent" />
                    <span>搜索个股查看详细财务数据</span>
                  </div>
                  <div className="w-full max-w-xl">
                    <StockFinancialSearch onSelect={pick} />
                  </div>
                  <div className="text-[11px] text-muted">支持股票代码或名称模糊匹配，如 600000 / 浦发</div>
                </div>
              )}
            </div>

            {/* 个股详情 / 空引导 */}
            <div className="pb-4">
              {selected ? (
                <StockFinancialDetail symbol={selected.symbol} name={selected.name} />
              ) : (
                <EmptyState
                  icon={Search}
                  title="未选择股票"
                  hint="在上方搜索框输入股票代码或名称，选择后即可查看该股的核心指标、财务报表与股本历史。"
                />
              )}
            </div>

            {/* AI 历史分析报告 */}
            {available && <ReportHistoryPanel />}
          </>
        )}
      </div>
    </>
  )
}
