// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import type { SentimentObservation, SentimentObservations as Data } from '@/lib/api'
import { SentimentObservationHelp, SentimentObservations, observationValue } from './SentimentObservations'
import { SentimentRadar } from './SentimentRadar'
const observation: SentimentObservation = {
  key: 'up_pct', label: '上涨比例', group: 'breadth', unit: '%', value: 61,
  denominator: 4000, status: 'available', percentile: 82, history_sample_count: 500,
  history_coverage: 1, change_5d: 5, extreme_streak: 0, extreme_side: null,
}
const data: Data = { version: 'observations-v1', as_of: '2026-10-15', universe: { exclude_st: true, suspension_rule: '剔除停牌' }, coverage: { sample_count: 4200, valid_change_count: 4000, method_current: true, is_complete_day: true }, observations: [observation], context: [], limitations: [] }
function text(markup: string) { const el = document.createElement('div'); el.innerHTML = markup; return el.textContent! }
it('uses supplied fixed-window percentile and preserves raw percent units and coverage', () => {
  const result = text(renderToStaticMarkup(<SentimentObservations data={data} expectedDate="2026-10-16" />))
  expect(result).toContain('61.0%')
  expect(result).toContain('P82')
  expect(result).toContain('+5.0个百分点')
  expect(result).toContain('有效涨跌 4000 家')
  expect(result).toContain('日期不同')
  expect(result).not.toContain('固定500个交易日')
  expect(result).not.toContain('收盘落盘不代表全市场覆盖')
})
it('shows observation methodology on keyboard focus and dismisses it with Escape', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const host = document.createElement('div')
  document.body.append(host)
  const root = createRoot(host)
  try {
    await act(async () => root.render(<SentimentObservationHelp data={data} />))
    const help = host.querySelector<HTMLButtonElement>('button[aria-label="状态观察口径"]')!
    expect(document.querySelector('[role="tooltip"]')).toBeNull()
    await act(async () => help.focus())
    const note = document.getElementById(help.getAttribute('aria-describedby')!)?.textContent
    expect(note).toContain('固定500个交易日')
    expect(note).toContain('常量序列为P50')
    expect(note).toContain('收盘落盘不代表全市场覆盖')
    await act(async () => help.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
    expect(document.querySelector('[role="tooltip"]')).toBeNull()
  } finally {
    await act(async () => root.unmount())
    host.remove()
  }
})
it('distinguishes missing data from insufficient history; old APIs cannot fabricate a score', () => {
  const partial = { ...data, observations: [{ ...observation, percentile: null, history_sample_count: 20 }, { ...observation, key: 'option_sentiment', label: '期权情绪', group: 'external' as const, value: null, percentile: null, missing_reason: '数据源未接入' }] }
  const result = text(renderToStaticMarkup(<SentimentObservations data={partial} />))
  expect(result).toContain('61.0%')
  expect(result).toContain('历史不足或不可比')
  expect(result).toContain('20/500日')
  expect(result).toContain('期权情绪：数据源未接入')
  expect(text(renderToStaticMarkup(<SentimentObservations />))).toContain('不可计算')
  expect(renderToStaticMarkup(<SentimentRadar data={partial} />)).not.toContain('<polygon')
})
it('formats ratios, amount multiples and board height with explicit units', () => {
  expect(observationValue({ ...observation, key: 'seal_rate', unit: 'ratio', value: .8 })).toBe('80.0%')
  expect(observationValue({ ...observation, key: 'amount_ratio_5_20', unit: 'ratio', value: 1.2 })).toBe('1.20倍')
  expect(observationValue({ ...observation, key: 'max_consecutive', unit: 'count', value: 8 })).toBe('8板')
  expect(observationValue({ ...observation, value: null })).toBe('不可计算')
})
it('shows intraday incompleteness without calling it a low or neutral state', () => {
  const result = text(renderToStaticMarkup(<SentimentObservations data={{ ...data, coverage: { ...data.coverage, is_complete_day: false } }} />))
  expect(result).toContain('收盘口径尚未确认')
  expect(result).not.toContain('冰点')
})
it('shows the amount repair reason in the compact dashboard without a hover', () => {
  const partial: Data = { ...data, observations: [{ ...observation, key: 'amount_ratio_5_20', label: '5日/20日成交额', group: 'activity', value: null, percentile: null, history_sample_count: 228, missing_reason: '需完整20日成交额，当前有效19/20日；2026-10-19收盘口径未确认，需补采正式日线后重算环境' }] }
  const result = text(renderToStaticMarkup(<SentimentObservations data={partial} compact />))
  expect(result).toContain('228/500日')
  expect(result).toContain('当前有效19/20日')
  expect(result).toContain('2026-10-19收盘口径未确认')
})
it('reports radar readiness and preserves complete percentile axes', () => {
  const keys = ['up_pct', 'above_ma20_pct', 'total_amount', 'index_position_60', 'limit_up_pct', 'seal_rate']
  const partial: Data = { ...data, observations: keys.map((key, i) => ({ ...observation, key, percentile: i === 0 ? 82 : null, history_sample_count: i === 0 ? 500 : 198 })) }
  const result = renderToStaticMarkup(<SentimentRadar data={partial} />)
  expect(text(result)).toContain('六项中1项已有完整500日分位')
  expect(text(result)).toContain('有效历史198–500日')
  expect(result).not.toContain('<polygon')
  expect(text(renderToStaticMarkup(<SentimentRadar data={{ ...partial, coverage: { ...data.coverage, is_complete_day: false } }} />))).toContain('当日收盘口径尚未确认')
  const complete = { ...data, observations: keys.map(key => ({ ...observation, key })) }
  expect(renderToStaticMarkup(<SentimentRadar data={complete} />)).toContain('<polygon')
})

it('labels a prior closing radar while keeping the latest unconfirmed data visible', () => {
  const keys = ['up_pct', 'above_ma20_pct', 'total_amount', 'index_position_60', 'limit_up_pct', 'seal_rate']
  const pending: Data = { ...data, as_of: '2026-11-10', coverage: { ...data.coverage, is_complete_day: false },
    observations: keys.map(key => ({ ...observation, key, percentile: null })) }
  const previous = { as_of: '2026-11-09', axes: keys.map(key => ({ key, label: key, percentile: 42 })) }
  const result = renderToStaticMarkup(<><SentimentRadar data={pending} previous={previous} /><SentimentObservations data={pending} compact /></>)
  expect(result).toContain('aria-label="2026-11-09 情绪雷达')
  expect(text(result)).toContain('雷达日期 2026-11-09')
  expect(text(result)).toContain('2026-11-10 收盘口径尚未确认')
  expect(text(result)).toContain('数据日期 2026-11-10')
  expect(text(result)).not.toContain('历史不足或不可比')
  expect(text(result)).toContain('P42')
  expect(result).toContain('<polygon')
  const confirmed: Data = { ...pending, coverage: { ...pending.coverage, is_complete_day: true },
    observations: keys.map(key => ({ ...observation, key, percentile: 82 })) }
  const settled = renderToStaticMarkup(<SentimentRadar data={confirmed} previous={previous} />)
  expect(settled).toContain('aria-label="2026-11-10 情绪雷达')
  expect(text(settled)).toContain('P82')
  expect(text(settled)).not.toContain('P42')
})
