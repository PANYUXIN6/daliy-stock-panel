// @vitest-environment jsdom
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import type { SentimentObservation, SentimentObservations as Data } from '@/lib/api'
import { SentimentObservations, observationValue } from './SentimentObservations'
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
  expect(result).toContain('固定500个交易日')
  expect(result).toContain('常量序列为P50')
  expect(result).toContain('收盘落盘不代表全市场覆盖')
})
it('distinguishes missing data from insufficient history; old APIs cannot fabricate a score', () => {
  const partial = { ...data, observations: [{ ...observation, percentile: null, history_sample_count: 20 }, { ...observation, key: 'option_sentiment', label: '期权情绪', group: 'external' as const, value: null, percentile: null, missing_reason: '数据源未接入' }] }
  const result = text(renderToStaticMarkup(<SentimentObservations data={partial} />))
  expect(result).toContain('61.0%')
  expect(result).toContain('历史不足或不可比')
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
