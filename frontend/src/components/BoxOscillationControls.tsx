import { useState } from 'react'
import type { BoxOscillation, KlinePeriod } from '@/lib/api'
import { BOX_LEVEL_LABELS, KLINE_PERIOD_LABELS, boxStatusLabel,
  type BoxLevelFilter, type BoxOscillationView, type BoxStatusFilter } from '@/lib/useBoxOscillation'

const selectClass = 'min-w-0 rounded border border-border bg-base px-1.5 py-1 text-[11px] text-secondary outline-none focus:border-accent'
const eventLabels = { breakout_pending: '越界待确认', breakout_confirmed: '突破确认', returned: '回到箱内' } as const
const price = (value: number) => Number(value).toFixed(2)

function BoxDetails({ box, allBoxes }: { box: BoxOscillation; allBoxes: BoxOscillation[] }) {
  const parent = allBoxes.find(item => item.id === box.parent_id)
  const firstBreakoutDate = box.events.find(event => event.type === 'breakout_pending')?.date
  return (
    <div className="mt-2 space-y-1.5 break-words text-[11px] leading-relaxed text-secondary">
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        <span>范围 {price(box.lower)}–{price(box.upper)}</span>
        <span>价差 {price(box.width)} · 宽度 {price(box.width_pct)}%</span>
        <span>{box.duration_bars} 根有效{KLINE_PERIOD_LABELS[box.period]}</span>
        <span>上沿 {box.upper_touches.length} 次 · 下沿 {box.lower_touches.length} 次独立试探</span>
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-muted">
        <span>起点 {box.start_date}</span><span>区间终点 {box.end_date}</span>
        <span>首次确认 {box.confirmed_date ?? '尚未确认'}</span><span>观察截止 {box.observed_until}</span>
      </div>
      <div>上沿触点：{box.upper_touches.map(touch => `${touch.date}（${price(touch.price)}，${touch.confirmed_date ? `${touch.confirmed_date} 确认` : '待确认'}）`).join('、') || '暂无'}</div>
      <div>下沿触点：{box.lower_touches.map(touch => `${touch.date}（${price(touch.price)}，${touch.confirmed_date ? `${touch.confirmed_date} 确认` : '待确认'}）`).join('、') || '暂无'}</div>
      {box.parent_id && <div>嵌套父箱：{parent ? `${BOX_LEVEL_LABELS[parent.level]} ${price(parent.lower)}–${price(parent.upper)}（${parent.start_date} 起）` : box.parent_id}</div>}
      {firstBreakoutDate && <div>首次越界 {firstBreakoutDate}</div>}
      {box.breakout.first_date && <div>本轮越界 {box.breakout.first_date} · 突破确认 {box.breakout.confirmed_date ?? '尚未确认'}</div>}
      {box.events.length > 0 && <div>事件：{box.events.map(event => `${event.date} ${event.direction === 'up' ? '向上' : '向下'}${eventLabels[event.type]}`).join('；')}</div>}
      {box.missing_conditions.length > 0 && <div className="text-amber-600 dark:text-amber-400">尚缺：{box.missing_conditions.join('；')}</div>}
    </div>
  )
}

/** 个股与指数共用周期切换、筛选和可核对的结构证据。 */
export function BoxOscillationControls({ view }: { view: BoxOscillationView }) {
  const [expanded, setExpanded] = useState(false)
  const { period, enabled, report, query, selectedBox } = view
  return (
    <div className="mb-1 min-w-0 px-1">
      <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
        <div className="inline-flex rounded border border-border p-0.5" role="group" aria-label="K线周期">
          {(Object.entries(KLINE_PERIOD_LABELS) as [KlinePeriod, string][]).map(([key, label]) => (
            <button key={key} type="button" aria-pressed={period === key} onClick={() => view.setPeriod(key)}
              className={`rounded px-2 py-0.5 ${period === key ? 'bg-accent/20 text-accent' : 'text-muted hover:text-secondary'}`}>{label}</button>
          ))}
        </div>
        <button type="button" aria-pressed={enabled} onClick={() => view.setEnabled(!enabled)}
          className={`rounded px-2 py-1 ${enabled ? 'bg-accent/20 text-accent' : 'bg-elevated text-muted'}`}>箱体</button>
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
        </>}
      </div>
      {view.rangeError && <div role="alert" className="py-1 text-[11px] text-danger">{view.rangeError}</div>}
      {enabled && <div role="status" className="flex flex-wrap gap-x-3 gap-y-1 py-1 text-[10px] text-muted">
        {query.isLoading && <span>识别箱体中…</span>}
        {query.isError && <><span className="text-danger">箱体读取失败</span><button type="button" className="text-accent" onClick={() => { void query.refetch() }}>重试</button></>}
        {report && <span>{report.status === 'no_prices' ? '缺少本地历史K线' : report.status === 'insufficient_data' ? '已完成K线不足，暂不能确认箱体' : view.filteredBoxes.length === 0 ? '当前范围与筛选条件下没有箱体' : `${selectedBox ? `${BOX_LEVEL_LABELS[selectedBox.level]} · ${boxStatusLabel(selectedBox)} · ${selectedBox.duration_bars} 根` : ''}`}</span>}
        {report && <span>已存历史 {report.history_start ?? '—'} 起 · 识别截止 {report.as_of ?? '—'}</span>}
        {report?.diagnostics.map((message, index) => <span key={`${index}:${message}`}>{message}</span>)}
        <span>按已完成{KLINE_PERIOD_LABELS[period]}识别；当前未完成K线仅供展示</span>
      </div>}
      {!enabled && period !== 'daily' && <div role="status" className="py-1 text-[10px] text-muted">
        当前未完成{KLINE_PERIOD_LABELS[period]}仅供展示。{report?.diagnostics.join('；')}
      </div>}
      {enabled && expanded && report && <div className="mb-2 rounded border border-border bg-elevated/40 p-2">
        {view.filteredBoxes.length > 0 && <div className="flex flex-wrap items-center gap-2">
          <select aria-label="选择箱体" value={selectedBox?.id ?? ''} onChange={event => view.setSelectedId(event.target.value)} className={`${selectClass} max-w-full`}>
            {view.filteredBoxes.map(box => <option key={box.id} value={box.id}>{BOX_LEVEL_LABELS[box.level]} · {boxStatusLabel(box)} · {box.start_date} · {price(box.lower)}–{price(box.upper)}</option>)}
          </select>
          <label className="flex items-center gap-1.5 text-[11px] text-muted"><input type="checkbox" checked={view.showAll} onChange={event => view.setShowAll(event.target.checked)} />叠加全部筛选结果</label>
          <span className="text-[10px] text-muted">默认仅显示所选箱体，避免重叠</span>
        </div>}
        {selectedBox ? <BoxDetails box={selectedBox} allBoxes={report.boxes} /> : <span className="text-[11px] text-muted">当前没有可展示的结构证据</span>}
      </div>}
    </div>
  )
}
