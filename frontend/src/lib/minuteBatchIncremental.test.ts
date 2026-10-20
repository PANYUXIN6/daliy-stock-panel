import { QueryClient } from '@tanstack/react-query'
import { expect, it, vi } from 'vitest'
import type { MinuteKlineRow } from './api'
import { fetchMinuteBatchIncremental, markMinuteBatchForFullRead } from './minuteBatchIncremental'
import { QK } from './queryKeys'

const mock = vi.hoisted(() => ({ batch: vi.fn() }))
vi.mock('./api', () => ({ api: { klineMinuteBatch: mock.batch } }))
const symbol = '600000.SH'
const other = '000001.SZ'
function row(time: string, close = 10): MinuteKlineRow {
  return { datetime: `2026-10-19 ${time}:00`, open: close, high: close, low: close, close, volume: 100, amount: close * 100 }
}

it('显式补采只触发一次完整读取，保留视口外缓存并随后恢复增量', async () => {
  const client = new QueryClient()
  const symbols = [symbol, other]
  const key = QK.minuteBatch(`${symbols.join(',')},300001.SZ`)
  const outside = [row('09:30'), row('15:00')]
  client.setQueryData(key, { data: { [other]: [row('14:59'), row('15:00')], '300001.SZ': outside } })
  const unrelated = QK.minuteBatch(other)
  client.setQueryData(unrelated, { data: { [other]: outside } })
  markMinuteBatchForFullRead(client, symbol, '2026-10-19')
  mock.batch.mockImplementation(async (_symbols, _date, _preferLocal, since) => ({
    data: { [symbol]: [row('09:30'), row('15:00')].filter(bar => !since || bar.datetime >= since), [other]: [row('15:00')] },
    incremental: !!since,
  }))
  const result = await fetchMinuteBatchIncremental(client, key, symbols, true)
  expect(result.data[symbol].map(bar => bar.datetime)).toEqual(['2026-10-19 09:30:00', '2026-10-19 15:00:00'])
  expect(result.data[other]).toEqual([row('15:00')])
  expect(result.data['300001.SZ']).toEqual(outside)
  expect(result.fullReadSymbols).toBeUndefined()
  expect(client.getQueryData(unrelated)).toEqual({ data: { [other]: outside } })
  client.setQueryData(key, result)
  await fetchMinuteBatchIncremental(client, key, symbols, true)
  expect(mock.batch).toHaveBeenLastCalledWith(symbols, undefined, true, '2026-10-19 15:00:00')
  client.clear()
})

it.each([undefined, []] as (MinuteKlineRow[] | undefined)[])('长期缺数股票（%s）没有补采标记时保留普通增量轮询', async missing => {
  const client = new QueryClient()
  const symbols = [symbol, other]
  const key = QK.minuteBatch(symbols.join(','))
  client.setQueryData(key, { data: { [other]: [row('15:00')], ...(missing ? { [symbol]: missing } : {}) } })
  markMinuteBatchForFullRead(client, symbol, '2026-10-19')
  mock.batch.mockResolvedValue({ data: { [other]: [row('15:00')] }, incremental: false })
  const full = await fetchMinuteBatchIncremental(client, key, symbols, true)
  client.setQueryData(key, full)
  mock.batch.mockResolvedValue({ data: { [other]: [row('15:00')] }, incremental: true })
  await fetchMinuteBatchIncremental(client, key, symbols, true)
  expect(mock.batch).toHaveBeenLastCalledWith(symbols, undefined, true, '2026-10-19 15:00:00')
  expect(full.fullReadSymbols).toBeUndefined()
  client.clear()
})

it('被补采股票在视口外时保留一次读取标记，进入请求范围才消费', async () => {
  const client = new QueryClient()
  const key = QK.minuteBatch(`${symbol},${other}`)
  client.setQueryData(key, { data: { [other]: [row('15:00')] } })
  markMinuteBatchForFullRead(client, symbol, '2026-10-19')
  mock.batch.mockResolvedValue({ data: { [other]: [row('15:00')] }, incremental: true })
  const outside = await fetchMinuteBatchIncremental(client, key, [other], true)
  expect(outside.fullReadSymbols).toEqual([symbol])
  expect(mock.batch).toHaveBeenLastCalledWith([other], undefined, true, '2026-10-19 15:00:00')
  client.setQueryData(key, outside)
  mock.batch.mockResolvedValue({ data: { [symbol]: [row('09:30')] }, incremental: false })
  const inside = await fetchMinuteBatchIncremental(client, key, [symbol], true)
  expect(inside.fullReadSymbols).toBeUndefined()
  expect(inside.data[other]).toEqual([row('15:00')])
  expect(mock.batch).toHaveBeenLastCalledWith([symbol], undefined, true, undefined)
  client.clear()
})

it('完整缓存继续按最旧尾根增量并覆盖合并', async () => {
  const client = new QueryClient()
  const symbols = [symbol, other]
  const key = QK.minuteBatch(`${symbols.join(',')},300001.SZ`)
  client.setQueryData(key, { data: {
    [symbol]: [row('09:30'), row('14:59')], [other]: [row('15:00')], '300001.SZ': [],
  } })
  mock.batch.mockResolvedValue({ data: { [symbol]: [row('14:59', 11), row('15:00', 12)] }, incremental: true })
  const result = await fetchMinuteBatchIncremental(client, key, symbols, true)
  expect(mock.batch).toHaveBeenLastCalledWith(symbols, undefined, true, '2026-10-19 14:59:00')
  expect(result.data[symbol].map(bar => bar.close)).toEqual([10, 11, 12])
  expect(result.data[other]).toHaveLength(1)
  client.clear()
})

it.each([false, true])('跨日全量响应整体替换旧日缓存（有补采标记：%s）', async marked => {
  const client = new QueryClient()
  const key = QK.minuteBatch(`${symbol},${other}`)
  client.setQueryData(key, { data: { [symbol]: [row('15:00')], [other]: [row('15:00')] } })
  if (marked) markMinuteBatchForFullRead(client, symbol, '2026-10-19')
  const newDay = { ...row('09:30'), datetime: '2026-10-20 09:30:00' }
  mock.batch.mockResolvedValue({ data: { [symbol]: [newDay] }, incremental: false })
  const result = await fetchMinuteBatchIncremental(client, key, [symbol], true)
  expect(result.data).toEqual({ [symbol]: [newDay] })
  expect(result.fullReadSymbols).toBeUndefined()
  client.clear()
})
