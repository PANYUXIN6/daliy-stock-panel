import { useState } from 'react'
import { MethodHelp } from '@/components/MethodHelp'
import type { BoxOscillation, BoxOscillationResponse, KlinePeriod } from '@/lib/api'
import { BOX_ACTIVITY_LABELS, BOX_LEVEL_LABELS, KLINE_PERIOD_LABELS, boxStatusLabel,
  type BoxLevelFilter, type BoxOscillationView, type BoxStatusFilter } from '@/lib/useBoxOscillation'

const selectClass = 'min-w-0 rounded border border-border bg-base px-1.5 py-1 text-[11px] text-secondary outline-none focus:border-accent'
const eventLabels = { breakout_pending: '越界待确认', breakout_confirmed: '突破确认', returned: '回到箱内' } as const
const price = (value: number) => Number(value).toFixed(2)
const activityReasons: Record<string, string> = {
  upper_stale: '上沿近期未验证', lower_stale: '下沿近期未验证', roundtrip_stale: '近期缺少完整往返',
  recent_roundtrip_required: '结构已成立，近期往返证据不足，等待新的完整往返',
}
const segmentEndReasons: Record<string, string> = {
  activity_downgraded: '往返证据老化', breakout_pending: '越界待确认，暂停填充',
  breakout: '确认突破', data_interrupted: '数据中断',
}
const calendarCoverageRule = '本地交易日历未覆盖全部历史，按已存有效交易记录计数，不补齐缺失 K 线。'
const completedKlineRule = '未完成的日／周／月 K 线仅供展示，不参与触点、箱体或突破确认。'

function BoxDetails({ box, allBoxes, config }: { box: BoxOscillation; allBoxes: BoxOscillation[]; config?: BoxOscillationResponse['config'] }) {
  const parent = allBoxes.find(item => item.id === box.parent_id)
  const previous = allBoxes.find(item => item.id === box.previous_box_id)
  const firstBreakoutDate = box.events.find(event => event.type === 'breakout_pending')?.date
  return (
    <div className="mt-2 space-y-1.5 break-words text-[11px] leading-relaxed text-secondary">
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        <span>范围 {price(box.lower)}–{price(box.upper)}</span>
        <span>价差 {price(box.width)} · 宽度 {price(box.width_pct)}%</span>
        <span>结构跨度 {box.duration_bars} 根有效{KLINE_PERIOD_LABELS[box.period]}（非连续活跃时长）</span>
        <span>上沿 {box.upper_touches.length} 次 · 下沿 {box.lower_touches.length} 次独立试探</span>
      </div>
      {box.lower_zone && box.upper_zone && <div className="flex flex-wrap gap-x-4 gap-y-1">
        <span>支撑区域 {price(box.lower_zone[0])}–{price(box.lower_zone[1])}</span>
        <span>压力区域 {price(box.upper_zone[0])}–{price(box.upper_zone[1])}</span>
        <span className="text-muted">图中边界为代表价格，区域内触碰允许有偏差</span>
      </div>}
      {config && box.confirmed_date && <div>突破确认阈值：下 {price(box.lower - (box.thresholds?.breakout_margin ?? box.width * config.breakout_margin_ratio))} · 上 {price(box.upper + (box.thresholds?.breakout_margin ?? box.width * config.breakout_margin_ratio))}；同方向连续 {config.breakout_closes} 根收盘越过才确认</div>}
      {box.thresholds && <div className="text-muted">{box.thresholds.atr == null
        ? 'ATR 预热不足，阈值采用箱高比例'
        : `波动参考：${box.thresholds.atr_date} 的 ATR ${price(box.thresholds.atr)}${box.confirmed_date ? '，确认后冻结' : '，候选种子参考'}`}</div>}
      {box.activity && <div>
        <div>{box.status === 'confirmed' ? '活跃性' : '结构结束时活跃性'}：{BOX_ACTIVITY_LABELS[box.activity.state]} · 状态自 {box.activity.state_change_date} · 证据截至 {box.activity.as_of}</div>
        <div>距上沿试探 {box.activity.upper_age_bars ?? '未知'} 根 · 距下沿试探 {box.activity.lower_age_bars ?? '未知'} 根 · 距完整往返确认 {box.activity.roundtrip_age_bars ?? '未知'} 根</div>
        {box.activity.reasons.length > 0 && <div className="text-muted">{box.activity.reasons.map(reason => activityReasons[reason] ?? '近期往返证据不足').join('；')}</div>}
      </div>}
      {box.active_segments?.map(segment => <div key={segment.id} className="text-muted">
        活跃填充：{segment.start_date}–{segment.end_date ?? box.activity?.as_of ?? box.observed_until}
        {segment.duration_bars != null ? ` · ${segment.duration_bars} 根有效K` : ''}
        {segment.end_reason ? `；${segment.end_event_date} ${segmentEndReasons[segment.end_reason]}` : '（当前延续）'}
      </div>)}
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-muted">
        <span>起点 {box.start_date}</span><span>区间终点 {box.end_date}</span>
        <span>首次确认 {box.confirmed_date ?? '尚未确认'}</span><span>观察截止 {box.observed_until}</span>
      </div>
      <div>上沿触点：{box.upper_touches.map(touch => `${touch.date}（${price(touch.price)}，${touch.confirmed_date ? `${touch.confirmed_date} 确认` : '待确认'}）`).join('、') || '暂无'}</div>
      <div>下沿触点：{box.lower_touches.map(touch => `${touch.date}（${price(touch.price)}，${touch.confirmed_date ? `${touch.confirmed_date} 确认` : '待确认'}）`).join('、') || '暂无'}</div>
      {box.parent_id && <div>嵌套父箱：{parent ? `${BOX_LEVEL_LABELS[parent.level]} ${price(parent.lower)}–${price(parent.upper)}（${parent.start_date} 起）` : box.parent_id}</div>}
      {previous && <div>同范围历史箱体：{previous.start_date}–{previous.end_date}；本次使用新的触点独立确认</div>}
      {firstBreakoutDate && <div>首次越界 {firstBreakoutDate}</div>}
      {box.breakout.first_date && <div>本轮越界 {box.breakout.first_date} · 突破确认 {box.breakout.confirmed_date ?? '尚未确认'}</div>}
      {box.events.length > 0 && <div>事件：{box.events.map(event => `${event.date} ${event.direction === 'up' ? '向上' : '向下'}${eventLabels[event.type]}`).join('；')}</div>}
      {box.status === 'broken' && <div className="text-muted">这段震荡已结束。虚线延伸仅为历史边界参考；空心菱形标记回访，后续回访不会延长原箱体。</div>}
      {!!box.revisits?.length && <div>历史回访：{box.revisits.map(visit => `${visit.date} ${visit.kind === 'entered' ? `从${visit.boundary === 'upper' ? '上' : '下'}方再次进入原区间` : `触及历史${visit.boundary === 'upper' ? '上' : '下'}沿区域`}`).join('；')}</div>}
      {box.missing_conditions.length > 0 && <div className="text-amber-600 dark:text-amber-400">尚缺：{box.missing_conditions.join('；')}</div>}
    </div>
  )
}

/** 个股与指数共用周期切换、筛选和可核对的结构证据。 */
export function BoxOscillationControls({ view }: { view: BoxOscillationView }) {
  const [expanded, setExpanded] = useState(false)
  const { period, enabled, report, query, nearestBox, selectedBox } = view
  const diagnostics = [...new Set(report?.diagnostics)].filter(note => note !== calendarCoverageRule && note !== completedKlineRule)
  const summary = report?.status === 'no_prices' ? '缺少本地历史K线'
    : report?.status === 'insufficient_data' ? '已完成K线不足，暂不能确认箱体'
    : nearestBox ? `活跃箱体 · ${BOX_LEVEL_LABELS[nearestBox.level]} · ${boxStatusLabel(nearestBox)} · 结构跨度 ${nearestBox.duration_bars} 根`
    : report && report.as_of_close == null && !report.rows.some(row => row.date === report.as_of) ? '缺少已完成收盘价，无法选出活跃箱体'
    : '暂无活跃箱体；可查看历史区间'
  return (
    <div className="mb-1 min-w-0 px-1">
      <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
        <div className="inline-flex rounded border border-border p-0.5" role="group" aria-label="K线周期">
          {(Object.entries(KLINE_PERIOD_LABELS) as [KlinePeriod, string][]).map(([key, label]) => (
            <button key={key} type="button" aria-pressed={period === key} onClick={() => view.setPeriod(key)}
              className={`rounded px-2 py-0.5 ${period === key ? 'bg-accent/20 text-accent' : 'text-muted hover:text-secondary'}`}>{label}</button>
          ))}
        </div>
        <div className="inline-flex items-center gap-0.5">
          <button type="button" aria-pressed={enabled} onClick={() => view.setEnabled(!enabled)}
            className={`rounded px-2 py-1 ${enabled ? 'bg-accent/20 text-accent' : 'bg-elevated text-muted'}`}>箱体</button>
          <MethodHelp label="箱体识别说明">
            <p>{completedKlineRule}</p>
            {report?.diagnostics.includes(calendarCoverageRule) && <p>{calendarCoverageRule}</p>}
            <p>默认仅显示收盘位于箱内、近期有完整往返证据的活跃箱体；多个符合时优先较窄的箱体。历史参考需主动开启。</p>
            <p>上→下→上或下→上→下才是完整往返。发生日决定新鲜度，确认日决定证据可用时间；越界待确认暂停填充，证据继续老化。</p>
            {report?.config?.activity_short_bars != null && <p>当前周期证据年龄超过 {report.config.activity_short_bars} 根降级，超过 {report.config.activity_long_bars} 根转历史参考；参数是展示初值。</p>}
            {report && <p>展示至少持续 {view.minDisplayBars} 根已完成{KLINE_PERIOD_LABELS[period]}的结构。</p>}
          </MethodHelp>
        </div>
        {enabled && <>
          <select aria-label="箱体大小" value={view.level} onChange={event => view.setLevel(event.target.value as BoxLevelFilter)} className={selectClass}>
            <option value="all">全部大小</option><option value="short">小箱体</option><option value="medium">中箱体</option><option value="long">大箱体</option>
          </select>
          <select aria-label="箱体状态" value={view.status} onChange={event => view.setStatus(event.target.value as BoxStatusFilter)} className={selectClass}>
            <option value="all">全部状态</option><option value="candidate">形成中候选</option><option value="confirmed">已形成</option><option value="broken">已突破</option><option value="invalidated">结构失效</option>
          </select>
          <button type="button" aria-expanded={expanded} onClick={() => setExpanded(value => !value)} className="rounded px-2 py-1 text-accent hover:bg-elevated">
            {expanded ? '收起详情' : '箱体详情'}{report ? `（${view.filteredBoxes.length}）` : ''}
          </button>
          <button type="button" aria-pressed={view.showHistory} onClick={() => view.setShowHistory(!view.showHistory)} className={`rounded px-2 py-1 ${view.showHistory ? 'bg-accent/20 text-accent' : 'text-muted hover:bg-elevated'}`}>历史参考</button>
        </>}
      </div>
      {view.rangeError && <div role="alert" className="py-1 text-[11px] text-danger">{view.rangeError}</div>}
      {(enabled || (period !== 'daily' && diagnostics.length > 0)) && <div role="status" className="flex flex-wrap gap-x-3 gap-y-1 py-1 text-[10px] text-muted">
        {enabled && query.isLoading && <span>识别箱体中…</span>}
        {enabled && query.isError && <><span className="text-danger">箱体读取失败</span><button type="button" className="text-accent" onClick={() => { void query.refetch() }}>重试</button></>}
        {enabled && report && <span>{summary}</span>}
        {enabled && view.showHistory && <span>历史参考已开启：主图跟随详情选择，虚线不表示当前震荡</span>}
        {enabled && view.pendingBoxes.map(box => <span key={box.id} className="text-warning">{price(box.lower)}–{price(box.upper)}：{boxStatusLabel(box)} · {box.activity ? BOX_ACTIVITY_LABELS[box.activity.state] : '活跃性未知'}{box.activity?.reasons.length ? `（${box.activity.reasons.map(reason => activityReasons[reason] ?? '近期往返证据不足').join('；')}）` : ''}</span>)}
        {enabled && report?.history_start && <span>已存历史 {report.history_start} 起{report.as_of ? ` · 识别截止 ${report.as_of}` : ''}</span>}
        {diagnostics.map(note => <span key={note} className="text-warning">{note}</span>)}
      </div>}
      {enabled && expanded && report && <div className="mb-2 rounded border border-border bg-elevated/40 p-2">
        {view.filteredBoxes.length > 0 && <div className="flex flex-wrap items-center gap-2">
          <select aria-label="选择箱体" value={selectedBox?.id ?? ''} onChange={event => view.setSelectedId(event.target.value)} className={`${selectClass} max-w-full`}>
            {view.filteredBoxes.map(box => <option key={box.id} value={box.id}>{BOX_LEVEL_LABELS[box.level]} · {boxStatusLabel(box)}{box.status === 'confirmed' && box.activity ? ` · ${BOX_ACTIVITY_LABELS[box.activity.state]}` : ''} · {box.start_date} · {price(box.lower)}–{price(box.upper)}</option>)}
          </select>
          <span className="text-[10px] text-muted">{view.showHistory ? '选择箱体切换主图历史参考与详情' : '选择箱体仅切换下方详情'}</span>
        </div>}
        {selectedBox ? <BoxDetails box={selectedBox} allBoxes={report.boxes} config={report.config} /> : <span className="text-[11px] text-muted">当前没有可展示的结构证据</span>}
      </div>}
    </div>
  )
}
