// @vitest-environment jsdom
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import type { MarketCohortPerformance, MarketSentimentClassification } from '@/lib/api'
import { SentimentClassification } from './SentimentClassification'

const sample: MarketCohortPerformance = {
  value: .01, unit: 'ratio', source_date: '2026-11-09', as_of: '2026-11-10',
  pool: 12, valid: 9, suspended: 2, missing: 1, limited: 3, complete: false, usable: false,
  missing_reason: '昨日集合或端点数据不完整', price_basis: '同一版本复权收盘价',
  data_version: 'fixture', rule_version: 'v1', formula: 'mean(close / previous_close - 1)',
}
const data: MarketSentimentClassification = {
  version: 'market-sentiment-v1', as_of: '2026-11-10', phase: 'rally',
  heat_level: 'high', quality_level: 'fragile', sentiment_direction: 'improving', sentiment_state: 'hot_fragile',
  direction_votes: [], evidence: { limit_up: 60, broken_rate: .35, seal_rate: .65,
    limit_down: 3, ge2_count: 12, promo_rate: .2, promo_pool: 15, limit_up_amount: 1e9,
    previous_limit_up_premium: sample, previous_broken_performance: sample },
  unknown_reasons: ['昨日集合或端点数据不完整'], universe: { exclude_st: true, method_version: 'observations-v1' },
}

it('shows independent heat, quality, direction and phase without treating hot-fragile as cold', () => {
  const markup = renderToStaticMarkup(<SentimentClassification data={data} />)
  const container = document.createElement('div')
  container.innerHTML = markup
  expect(container.textContent).toContain('高热但脆弱')
  expect(container.textContent).toContain('正在改善')
  expect(container.textContent).toContain('温度高')
  expect(container.textContent).toContain('封板质量脆弱')
  expect(container.textContent).toContain('短线活跃')
  expect(container.textContent).not.toContain('冷清')
  expect(container.textContent).toContain('35.0%')
  expect(container.textContent).toContain('有效 9 / 原集合 12')
  expect(container.textContent).toContain('停牌 2，缺数 1，有成交限制 3')
  expect(container.querySelector('details summary')?.textContent).toBe('判定依据与样本')
})

it('preserves missing values and old report state instead of producing zero statistics', () => {
  expect(renderToStaticMarkup(<SentimentClassification />)).toContain('旧版未记录')
  const markup = renderToStaticMarkup(<SentimentClassification data={{ ...data,
    sentiment_state: 'unknown', evidence: { ...data.evidence, broken_rate: null,
      previous_limit_up_premium: { ...sample, value: null } } }} compact />)
  expect(markup).toContain('综合判断待核验')
  expect(markup).toContain('—')
  expect(markup).toContain('（待核验）')
  expect(markup).not.toContain('0.0%')
})
