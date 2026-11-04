import type { ReactNode } from 'react'
import type { EveningPath, EveningRecord, EveningReport } from '@/lib/api'

/** 只消费归档入选顺序；不从完整候选数据中补名额或重新排名。 */
export function selectedReportStocks(report: EveningReport): EveningRecord[] {
  const bySymbol = new Map(report.records.map(row => [row.symbol, row]))
  return [...new Set(report.selected)].slice(0, 5).flatMap(symbol => {
    const row = bySymbol.get(symbol)
    return row ? [row] : []
  })
}

const plainReasons: Record<string, string> = {
  完整核心或候选路径: '尚无满足观察条件的股票',
  没有完整核心或候选资格: '尚无满足观察条件的股票',
  当日位置满足: '价格位置条件未满足',
  观察位置条件满足: '观察所需的价格位置条件未满足',
  全股主要用途允许精选: '尚无满足全部观察条件的股票',
  当前风险核验通过: '风险条件尚未核验通过',
  无未解除暂停: '价格风险尚未解除',
  市场政策通过: '市场条件尚不满足',
  板块活跃: '题材走势尚不满足',
  韧性无观察限制: '抗跌表现仍待验证',
  '压力进行中，成熟历史不能掩盖当前冲击': '压力尚未结束，继续观察后续抗跌与修复表现',
}
function plain(text: string): string {
  return (plainReasons[text] ?? text)
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

function selectionReason(path: EveningPath | undefined): string {
  if (!path) return '本期筛选入选，详细理由未记录。'
  const reasons: Record<string, string> = {
    board: '因近期涨停表现入选，继续观察后续走势。',
    trend: '因近期趋势表现入选，继续观察走势能否延续。',
    candidate: '近期出现走强变化，列入后续观察。',
    catchup: '同题材出现后续走强迹象，列入后续观察。',
  }
  return reasons[path.selection_role ?? ''] ?? '本期筛选入选，详细理由未记录。'
}
function number(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}
function metrics(path: EveningPath | undefined): string[] {
  const f = path?.features ?? {}
  const close = number(f.raw_close)
  const r5 = number(f.R5)
  const r20 = number(f.R20)
  const amount = number(f.amount)
  const pct = (value: number) => `${value > 0 ? '+' : ''}${(value * 100).toFixed(2)}%`
  return [
    close === undefined ? '收盘价未记录' : `收盘 ${close.toFixed(2)}元`,
    r5 === undefined ? '5日涨幅未记录' : `5日 ${pct(r5)}`,
    r20 === undefined ? '20日涨幅未记录' : `20日 ${pct(r20)}`,
    amount === undefined ? '当日成交额未记录' : `成交额 ${(amount / 100_000_000).toFixed(2)}亿`,
  ]
}
function limitations(row: EveningRecord, path: EveningPath | undefined): string[] {
  return uniqueReasons([
    ...row.limitations,
    ...(path?.limitations ?? []),
    ...(path?.calculation_reasons ?? []),
    ...(path?.selection.unknown_reasons ?? []),
    ...(path?.resilience.unknown_reasons ?? []),
    ...row.risk.judgement.unknown_reasons,
    ...row.position.unknown_reasons,
    ...(row.position.value === false ? row.position.failed_reasons.map(reason => reason === '不过热' ? '位置过热：仅作后续跟踪，留意价格回落' : `位置条件未满足：${reason}`) : []),
    ...(row.risk.risk_now === true ? ['单日价格风险已触发'] : []),
    ...(row.risk.paused === true ? ['价格风险尚未解除'] : []),
  ])
}

/** 纯展示，生产页面提供账号操作；离线预览无需挂载查询或连接后台。 */
export function EveningReportContent({ report, renderActions, reportNotes }: {
  report: EveningReport
  reportNotes?: string[]
  renderActions?: (row: EveningRecord, selected: EveningRecord[]) => ReactNode
}) {
  const selected = selectedReportStocks(report)
  const emptyReasons = uniqueReasons(report.statistics.zero_selection_reasons)
  return <section className="space-y-4" aria-label="后续观察名单">
    <header className="space-y-1">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="text-lg font-semibold text-foreground">{report.trade_date}</h2>
        <span className="text-sm text-secondary">{selected.length}只观察 · 最多5只</span>
      </div>
      <p className="text-sm text-secondary">{marketSentence(report.market)} 下个交易日：{report.next_trade_date ?? '待确认'}。</p>
      <p className="text-xs leading-5 text-secondary">{classificationSentence(report.provenance)}</p>
      {(report.late_generation || report.recovery_note) && <p className="text-xs text-warning">{report.recovery_note ?? '迟到生成'} · 实际生成时间：{report.generated_at}</p>}
    </header>

    {selected.length === 0 ? <div className="rounded-lg border border-border bg-surface px-5 py-6">
      <p className="font-medium text-foreground">暂无符合条件的观察股票</p>
      <p className="mt-2 text-sm leading-6 text-secondary">{emptyReasons.length ? emptyReasons.join('；') : '本期观察名单为空，具体原因未记录。'}</p>
    </div> : <div className="divide-y divide-border rounded-lg border border-border bg-surface px-4 sm:px-5">
      {selected.map(row => {
        const path = row.paths.find(item => item.selected_path === true)
        const notes = limitations(row, path)
        const waiting = uniqueReasons(row.wait_conditions ?? [])
        return <article key={row.symbol} aria-label={`${row.name}观察股票`} className="space-y-1.5 py-4 text-sm leading-6">
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
            <div className="flex min-w-0 flex-wrap items-baseline gap-x-2">
              <h3 className="text-[16px] font-semibold text-foreground">{row.name}</h3>
              <span className="font-mono text-xs text-muted">{row.symbol}</span>
              {path?.sector_name && <span className="text-xs text-secondary">{path.sector_name}</span>}
            </div>
            {renderActions?.(row, selected)}
          </div>
          <p className="text-foreground">{selectionReason(path)}</p>
          <p className="flex flex-wrap gap-x-4 gap-y-0 font-mono text-xs text-secondary">{metrics(path).map(metric => <span key={metric}>{metric}</span>)}</p>
          {notes.length > 0 && <p className="break-words text-warning"><span className="font-medium">注意：</span>{notes.join('；')}</p>}
          <p className="break-words text-secondary"><span className="font-medium">后续观察：</span>{waiting.length ? waiting.join('；') : '具体观察条件未记录，不能视为已满足。'}</p>
        </article>
      })}
    </div>}
    {(reportNotes ?? report.limitations ?? []).length > 0 && <p className="text-xs leading-5 text-secondary">报告说明：{uniqueReasons(reportNotes ?? report.limitations ?? []).join('；')}</p>}
    <p className="text-xs leading-5 text-muted">名单用于跟踪后续变化，不代表盈利预期或买点，不构成交易指令；未核验隔夜新闻公告。行情窗口展示当前行情，报告中的数据固定于所选日期。</p>
  </section>
}
