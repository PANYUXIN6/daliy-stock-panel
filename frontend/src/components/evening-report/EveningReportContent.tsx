import { useId, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import type { EveningRecord, EveningReport, EveningPersonalRecord, MarketSentimentClassification } from '@/lib/api'
import type { ColumnConfig } from '@/lib/list-columns'
import { fmtPct, fmtPrice, fmtVolume, priceColorClass } from '@/lib/format'
import { StockDataTable } from '@/components/stock-table/StockDataTable'
import { useTableSort } from '@/components/stock-table/useTableSort'
import { SentimentClassification } from '@/components/SentimentClassification'
import { Modal } from '@/components/Modal'
import { EveningFilters, emptyEveningFilters, matchesEveningFilters, roleLabels, boardLabels, dipLabel, eveningControl } from './EveningFilters'
import { EveningAllocationRank, EveningObservationDetails, EveningPerformanceDetails } from './EveningObservationDetails'

/** 只消费归档入选顺序；不从完整候选数据中补名额或重新排名。 */
export function selectedReportStocks(report: EveningReport): EveningRecord[] {
  const bySymbol = new Map(report.records.map(row => [row.symbol, row]))
  return [...new Set(report.selected)].flatMap(symbol => {
    const row = bySymbol.get(symbol)
    return row ? [row] : []
  })
}

function plain(text: string): string {
  return text
    .replace(/MA(\d+)/g, '$1日均线')
    .replace(/ATR(?:_?\d+)?/g, '日常波动参考')
    .replace(/完整比较组|完整组/g, '同题材股票')
    .replace(/核心身份|身份/g, '跟踪判断')
    .replace(/地位分/g, '走势比较分数')
    .replace(/轮次/g, '题材阶段')
    .replace(/韧性/g, '抗跌表现')
}
function uniqueReasons(reasons: string[]) { return [...new Set(reasons.filter(Boolean).map(plain))] }

function marketSentence(market: EveningReport['market']): string {
  const states: Record<string, string> = { strong: '强势', lean_strong: '偏强', neutral: '中性', lean_weak: '偏弱', weak: '弱势' }
  const phases: Record<string, string> = { ice: '短线低迷', ignite: '短线启动', rally: '短线活跃', climax: '短线高热', ebb: '短线退潮', repair: '短线修复' }
  const state = typeof market.state === 'string' ? states[market.state] : undefined
  const phase = typeof market.phase === 'string' ? phases[market.phase] : undefined
  const background = state ? `市场${state}` : '市场状态暂不明确'
  const unclassified = market.phase_reason === 'no_rule_match'
    ? '阶段未归类：当前未命中阶段规则'
    : market.phase_reason === 'pending_confirmation'
      ? '阶段候选待确认'
      : '阶段未归类，具体原因未记录'
  const phaseDescription = phase ?? (market.phase === 'unclassified'
    ? unclassified
    : market.phase === 'unknown' ? '阶段未知：核心输入不足' : '阶段信息暂不可用')
  return `${market.state === 'unavailable' ? '市场状态不可用' : background}；${phaseDescription}。`
}
function classificationSentence(provenance: EveningReport['provenance']): string {
  const observations = provenance.classifications
  if (!Array.isArray(observations) || observations.length === 0) return '分类口径：分类观察时点未记录'
  const effectiveDates = observations.map((observation: unknown) => {
    if (!observation || typeof observation !== 'object' || !('source_effective_date' in observation)) return null
    const date = observation.source_effective_date
    return typeof date === 'string' && date.trim() ? date.trim() : null
  })
  const knownDates = [...new Set(effectiveDates.filter((date): date is string => date !== null))]
  if (knownDates.length === 0) return '分类口径：本轮观察，源生效日未知'
  return `分类口径：本轮观察，分类截至${knownDates.join('、')}${effectiveDates.includes(null) ? '；部分来源生效日未知' : ''}`
}

const numericColumns = [
  ['raw_close', '报告日收盘'], ['change_pct', '当日涨跌幅'], ['return_5d', '5日涨幅'],
  ['return_20d', '20日涨幅'], ['identity_score', '辨识度'], ['amount_day', '成交额'],
]
const columns: ColumnConfig[] = [
  { id: 'stock', source: { type: 'builtin', key: 'symbol' }, label: '股票', visible: true },
  { id: 'theme', source: { type: 'computed', key: 'theme' }, label: '题材 / 角色', visible: true },
  ...numericColumns.map(([id, label]): ColumnConfig => ({ id, label, source: { type: 'computed', key: id }, visible: true, align: 'right' })),
  { id: 'reason', source: { type: 'computed', key: 'reason' }, label: '观察依据 / 低吸', visible: true },
  { id: 'risk', source: { type: 'computed', key: 'risk' }, label: '风险与待核验', visible: true },
  { id: 'feedback', source: { type: 'computed', key: 'feedback' }, label: '我的记录', visible: true },
]
const sortableColumns = new Set(numericColumns.map(([id]) => id))
function sortValue(row: EveningRecord, column: ColumnConfig) {
  const value = row.observation.values[column.id]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}
function riskNotes(row: EveningRecord) { return uniqueReasons(row.observation.risks ?? []) }

/** 浏览筛选和排序仅处理归档中的最终名单；个人状态由外层批量加载。 */
export function EveningReportContent({ report, renderActions, reportNotes, onOpenStock, onOpenDetails, personalRecords = [], renderFeedback }: {
  report: EveningReport
  reportNotes?: string[]
  renderActions?: (row: EveningRecord, selected: EveningRecord[], closeDetails: () => void) => ReactNode
  onOpenStock?: (row: EveningRecord, selected: EveningRecord[]) => void
  onOpenDetails?: () => void
  personalRecords?: EveningPersonalRecord[]
  renderFeedback?: (row: EveningRecord) => ReactNode
}) {
  const selected = selectedReportStocks(report)
  const [filters, setFilters] = useState({ ...emptyEveningFilters })
  const [detail, setDetail] = useState<string | null>(null)
  const detailTitleId = useId()
  const sort = useTableSort<EveningRecord>(sortValue)
  const feedback = new Map(personalRecords.map(record => [record.symbol, record]))
  // 首版定稿只有当时的入选身份与说明，没有观察变量或低吸明细。
  if (report.records.some(row => !row.observation)) {
    return <section aria-label="历史晚报" className="space-y-3">
      <h2 className="font-semibold">{report.trade_date}</h2><p>{report.summary}</p>
      <ol className="space-y-2">{selected.map(row => <li key={row.symbol}>
        <button className="text-accent" onClick={() => onOpenStock?.(row, selected)}>{row.name} · {row.symbol}</button>
        {row.purpose && <span className="ml-3">{row.purpose}</span>}
        {renderFeedback?.(row)}
      </li>)}</ol>
    </section>
  }
  const themeNames = new Map<string, string>()
  for (const sector of report.sectors) {
    const count = sector.observation?.selected_count
    if (sector.name && typeof count === 'number') {
      themeNames.set(sector.id, count === 0 ? `${sector.name}（已入围，0只入选）` : `${sector.name}（${count}只入选）`)
    }
  }
  for (const row of selected) {
    const theme = row.observation.primary_theme
    if (theme?.id && !themeNames.has(theme.id)) themeNames.set(theme.id, theme.name)
  }
  const themes = Array.from(themeNames, ([id, name]) => ({ id, name }))
  const selectedTheme = report.sectors.find(sector => sector.id === filters.theme)?.observation
  const themeDay = Array.isArray(selectedTheme?.daily)
    ? selectedTheme.daily.find(day => day.date === report.trade_date) : undefined
  const visible = sort.sortRows(selected.filter(row => matchesEveningFilters(row, filters, feedback.get(row.symbol)?.feedback)), columns)
  const detailRow = selected.find(row => row.symbol === detail)
  function openDetails(symbol: string) {
    setDetail(symbol)
    onOpenDetails?.()
  }
  const closeDetails = () => setDetail(null)
  const emptyReasons = uniqueReasons(report.statistics.zero_selection_reasons)
  const sentiment = report.market.sentiment as { classification?: MarketSentimentClassification } | undefined
  const classification = sentiment?.classification
  const renderCell = (row: EveningRecord, column: ColumnConfig) => {
    const observation = row.observation
    const dipReason = observation.dip.status === 'full' ? null : observation.dip.primary_reason
    const className = 'px-3 py-3 align-top'
    if (column.id === 'stock') return <td className={`${className} sticky left-0 z-[1] w-32 min-w-32 max-w-32 bg-surface sm:w-40 sm:min-w-40 sm:max-w-40`} data-report-stock={row.symbol}>
      <button className="block max-w-[104px] truncate text-left font-semibold text-foreground underline-offset-4 hover:text-accent hover:underline focus-visible:outline-accent sm:max-w-40" onClick={() => onOpenStock ? onOpenStock(row, visible) : openDetails(row.symbol)} aria-label={`${row.name}查看当前行情`}>{row.name}</button>
      <div className="mt-1 font-mono text-xs text-muted">{row.symbol}</div><div className="mt-1 text-[11px] text-secondary">{boardLabels[observation.board ?? ''] ?? '交易板块待核验'}</div>
      <div className="mt-1 text-[11px] text-secondary sm:hidden">{roleLabels[observation.role ?? ''] ?? '角色待核验'} · {dipLabel(observation.dip)}{dipReason && <span className="block">{dipReason}</span>}</div>
    </td>
    if (column.id === 'theme') return <td className={`${className} min-w-36`}><div className="max-w-44 break-words font-medium">{row.observation.primary_theme?.name ?? '题材未记录'}</div><div className="mt-1 text-xs text-secondary">{roleLabels[observation.role ?? ''] ?? '角色待核验'}</div>{observation.role_status === 'review_pending' && <span className="text-xs text-warning">跟踪待复核</span>}</td>
    if (column.id === 'feedback') return <td className={`${className} sticky right-0 z-[1] min-w-[156px] bg-surface shadow-[-1px_0_0_0_hsl(var(--border))]`}>{renderFeedback?.(row) ?? <span className="text-xs text-muted">个人记录未加载</span>}</td>
    if (column.id === 'reason') return <td className={`${className} min-w-60 max-w-72`}><p className="text-xs leading-5">{observation.reason}</p><p className="mt-1 text-xs text-secondary">低吸：{dipLabel(observation.dip)}{dipReason && ` · ${dipReason}`}</p>
      <EveningAllocationRank observation={observation} /><button className="mt-1 min-h-8 text-xs font-medium text-accent underline-offset-4 hover:underline" aria-haspopup="dialog" onClick={() => openDetails(row.symbol)}>查看详情</button></td>
    if (column.id === 'risk') { const notes = riskNotes(row); return <td className={`${className} min-w-56 max-w-72`}><p className="text-xs leading-5 text-warning">{notes.slice(0, 2).join('；') || '无新增价格风险提示'}</p>{notes.length > 2 && <button className="min-h-8 text-xs text-accent" aria-haspopup="dialog" onClick={() => openDetails(row.symbol)}>另有 {notes.length - 2} 项，查看详情</button>}</td> }
    const value = sortValue(row, column)
    const percent = column.id === 'change_pct' || column.id.startsWith('return_')
    return <td className={`${className} whitespace-nowrap text-right font-mono text-xs ${percent ? priceColorClass(value) : 'text-foreground'}`} title={value === null ? '必要数据缺失，不能补零' : undefined}>{value === null ? '—' : percent ? fmtPct(value) : column.id === 'amount_day' ? fmtVolume(value) : column.id === 'raw_close' ? fmtPrice(value) : value.toFixed(0)}</td>
  }
  return <section className="space-y-4" aria-label="后续观察名单">
    <header className="space-y-1">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="text-lg font-semibold text-foreground">{report.trade_date}</h2>
        <span className="text-sm text-secondary">{selected.length}只观察 · 最多5个题材，每题材2龙头+1情绪股</span>
      </div>
      <p className="text-sm text-secondary">{marketSentence(report.market)} 下个交易日：{report.next_trade_date ?? '待确认'}。</p>
      <p className="text-xs leading-5 text-secondary">{classificationSentence(report.provenance)}{report.industry_status === 'missing' && ' · 行业缺失'}</p>
      {(report.late_generation || report.recovery_note) && <p className="text-xs text-warning">{report.recovery_note ?? '迟到生成'} · 实际生成时间：{report.generated_at}</p>}
    </header>
    {classification ? <SentimentClassification data={classification} /> : <p className="text-xs text-muted">情绪分类资料暂不可用。</p>}
    <EveningFilters value={filters} onChange={setFilters} themes={themes} />
    {selectedTheme && <p className="text-xs text-secondary">
      当日涨停 {themeDay?.limit_up ?? '未记录'} 只 / 完整成员 {typeof selectedTheme.member_count === 'number' ? selectedTheme.member_count : '未记录'} 只
      {' · '}近5日强度 {typeof selectedTheme.strength === 'number' ? Number(selectedTheme.strength.toFixed(2)) : '未记录'}
      {' · '}{typeof selectedTheme.missing_count === 'number'
        ? `近5日缺少 ${selectedTheme.missing_count} 条成员日行情` : '近5日缺数未记录'}
    </p>}
    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-secondary"><span role="status">当前显示 {visible.length} / 本报告 {selected.length} 只</span><button className={eveningControl} disabled={!sort.sort} onClick={sort.reset}>恢复原报告顺序</button></div>
    {selected.length === 0 ? <div className="rounded-lg border border-border bg-surface px-5 py-6">
      <p className="font-medium text-foreground">暂无符合条件的观察股票</p>
      <p className="mt-2 text-sm leading-6 text-secondary">{emptyReasons.length ? emptyReasons.join('；') : '本期观察名单为空，具体原因未记录。'}</p>
    </div> : visible.length === 0 ? <div className="rounded-lg border border-border bg-surface px-5 py-8 text-center"><p>没有符合当前筛选的股票</p><p className="mt-2 text-xs text-secondary">本报告仍有 {selected.length} 只观察股票，清除筛选可恢复显示。</p></div> : <StockDataTable columns={columns} rows={visible} renderCell={renderCell} sort={sort.sort} onSortToggle={sort.toggle} sortableColumnIds={sortableColumns} minWidth={1580} className="overflow-x-auto rounded-lg border border-border bg-surface [&_th:first-child]:sticky [&_th:first-child]:left-0 [&_th:first-child]:z-[2] [&_th:first-child]:bg-elevated [&_th:last-child]:sticky [&_th:last-child]:right-0 [&_th:last-child]:z-[2] [&_th:last-child]:bg-elevated" />}
    {detailRow && createPortal(<Modal key={detailRow.symbol} onClose={closeDetails} labelledBy={detailTitleId}
      panelClassName="flex max-h-[85dvh] w-[calc(100vw-2rem)] max-w-4xl flex-col overflow-hidden break-words rounded-xl border border-border bg-surface text-foreground shadow-xl">
      <header className="flex shrink-0 items-start justify-between gap-4 border-b border-border p-4 sm:px-6 sm:py-5">
        <div className="min-w-0"><h3 id={detailTitleId} className="text-base font-semibold text-foreground sm:text-lg">{detailRow.name} · 观察详情</h3><p className="mt-1 text-xs text-secondary">{detailRow.symbol} · 报告日期 {report.trade_date}</p></div>
        <button className={`${eveningControl} shrink-0`} onClick={closeDetails}>关闭详情</button>
      </header>
      <div className="min-h-0 space-y-5 overflow-y-auto overscroll-contain p-4 sm:p-6">
        <EveningObservationDetails observation={detailRow.observation} definitions={report.variable_definitions} />
        {!!riskNotes(detailRow).length && <div className="text-sm leading-6 text-warning"><p className="font-medium">风险与待核验</p><ul className="list-inside list-disc">{riskNotes(detailRow).map(reason => <li key={reason}>{reason}</li>)}</ul></div>}
        {!!detailRow.wait_conditions?.length && <p className="text-sm text-secondary">后续观察：{uniqueReasons(detailRow.wait_conditions).join('；')}</p>}
        {feedback.get(detailRow.symbol) && <EveningPerformanceDetails record={feedback.get(detailRow.symbol)!} />}
      </div>
      {renderActions && <footer className="shrink-0 border-t border-border p-4 sm:px-6">{renderActions(detailRow, visible, closeDetails)}</footer>}
    </Modal>, document.body)}
    {(reportNotes ?? report.limitations ?? []).length > 0 && <p className="text-xs leading-5 text-secondary">报告说明：{uniqueReasons(reportNotes ?? report.limitations ?? []).join('；')}</p>}
    <p className="text-xs leading-5 text-muted">名单用于跟踪后续变化，不代表盈利预期或买点，不构成交易指令；未核验隔夜新闻公告。行情窗口展示当前行情，报告中的数据固定于所选日期。</p>
  </section>
}
