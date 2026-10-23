// @vitest-environment jsdom
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import type { AlertEvent } from '@/lib/api'
import { AlertConditionDetails } from './AlertConditionDetails'

const event: AlertEvent = {
  ts: 1, source: 'signal', type: 'signal', message: '提醒', logic: 'or', price: 10,
  conditions: [
    { field: 'csg_custom', op: 'truth' },
    { field: 'close', op: '>=', value: 9 },
    { field: 'rsi_14', op: '>=', value: 80 },
  ],
  signals: ['csg_custom'],
}

function render(value: AlertEvent) {
  return renderToStaticMarkup(<AlertConditionDetails event={value} customNames={{ csg_custom: '自定义信号' }} fallback="提醒正文" />)
}

it('shows the full matched subset first and the configured OR rule separately', () => {
  const html = render({ ...event, matched_conditions: event.conditions!.slice(0, 2) })
  const [hit, rule] = html.split('规则')
  expect(hit).toContain('自定义信号')
  expect(hit).toContain('&gt;=9')
  expect(hit).toContain('且')
  expect(hit).not.toContain('RSI14')
  expect(hit).not.toContain('或')
  expect(rule).toContain('&gt;=80')
  expect(rule).toContain('或')
})

it('keeps old truth-signal and pure-comparison records readable', () => {
  const [hit, rule] = render(event).split('规则')
  expect(hit).toContain('自定义信号')
  expect(hit).not.toContain('&gt;=9')
  expect(rule).toContain('&gt;=80')
  const legacy = render({ ...event, signals: [], matched_conditions: undefined })
  expect(legacy).toContain('&gt;=9')
  expect(legacy).toContain('&gt;=80')
  expect(legacy).toContain('或')
  expect(legacy).toContain('规则')
  expect(legacy).not.toContain('命中')
})

it('keeps strategy messages and custom text when there are no condition details', () => {
  expect(render({ ...event, source: 'strategy', conditions: [], signals: [] })).toContain('提醒正文')
})
