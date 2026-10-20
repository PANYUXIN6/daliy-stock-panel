// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QK } from '@/lib/queryKeys'
import { fetchMinuteBatchIncremental } from '@/lib/minuteBatchIncremental'
import { StockIntradayChart } from './StockIntradayChart'

const mocks = vi.hoisted(() => ({ minute: vi.fn(), batch: vi.fn(), fetch: vi.fn(), job: vi.fn(), adminJob: vi.fn(), matrix: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: {
  klineMinute: mocks.minute,
  klineMinuteBatch: mocks.batch,
  minuteFetch: mocks.fetch,
  minuteFetchJob: mocks.job,
  pipelineJob: mocks.adminJob,
  capabilityMatrix: mocks.matrix,
} }))
vi.mock('@/components/EChartsIntraday', () => ({ EChartsIntraday: ({ data }: { data: { close: number }[] }) => <div data-chart>{data[0].close}</div> }))

let root: Root
let host: HTMLDivElement
let client: QueryClient
const symbol = '600000.SH'
const date = '2026-10-19'
const running = { id: 'fetch-1', status: 'running', progress: 40, log: [{ msg: '正在获取分钟数据' }], result: null }
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  mocks.minute.mockResolvedValue({ symbol, date, rows: [], source: 'none' })
  mocks.matrix.mockResolvedValue({ capabilities: [{ id: 'minute', usable: true }] })
  mocks.fetch.mockResolvedValue({ status: 'started', symbol, date, job_id: 'fetch-1', reused: false })
  mocks.job.mockResolvedValue(running)
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})
async function settle() {
  for (let index = 0; index < 6; index++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function render(nextSymbol = symbol, nextDate = date) {
  await act(async () => root.render(<MemoryRouter><QueryClientProvider client={client}>
    <StockIntradayChart symbol={nextSymbol} date={nextDate} />
  </QueryClientProvider></MemoryRouter>))
  await settle()
}
function button(text: string) {
  const found = [...host.querySelectorAll('button')].find(item => item.textContent?.trim() === text)
  if (!found) throw new Error(`找不到按钮 ${text}`)
  return found
}
async function click(text: string) {
  await act(async () => button(text).click())
  await settle()
}

it('空态读取和刷新不自动采集，显式点击只提交当前股票和日期，等待期间挡住重复点击', async () => {
  let resolve!: (value: unknown) => void
  mocks.fetch.mockReturnValue(new Promise(done => { resolve = done }))
  await render()
  await click('刷新')
  expect(mocks.fetch).not.toHaveBeenCalled()
  const collect = button('采集该日分钟数据')
  await act(async () => { collect.click(); collect.click() })
  await settle()
  expect(mocks.fetch).toHaveBeenCalledTimes(1)
  expect(mocks.fetch).toHaveBeenCalledWith(symbol, date)
  expect(button('提交中…').disabled).toBe(true)
  await act(async () => resolve({ status: 'started', symbol, date, job_id: 'fetch-1', reused: true }))
  await settle()
  expect(mocks.job).toHaveBeenCalledWith('fetch-1')
  expect(mocks.adminJob).not.toHaveBeenCalled()
  expect(host.textContent).toContain('已有采集任务，已加入进度跟踪')
  expect(host.textContent).toContain('40%')
  expect(button('采集中…').disabled).toBe(true)
  expect(host.textContent).not.toContain('取消任务')
})

it('任务完成精确刷新该股票分钟缓存并自动显示图表', async () => {
  const related = [QK.klineMinute(symbol, ''), QK.klineMinuteRange(symbol, 5), QK.minuteBatch(`000001.SZ,${symbol}`)]
  const unrelated = [QK.klineMinute(symbol, '2026-10-16'), QK.klineMinute('000001.SZ', date), QK.klineMinuteRange('000001.SZ', 5), QK.minuteBatch('000001.SZ'), QK.klineLatest(symbol)]
  for (const key of [...related, ...unrelated]) client.setQueryData(key, {})
  await render()
  await click('采集该日分钟数据')
  mocks.minute.mockResolvedValue({ symbol, date, source: 'local', rows: [{ datetime: `${date} 09:35:00`, close: 12 }] })
  mocks.job.mockResolvedValue({ ...running, status: 'succeeded', progress: 100, result: { rows: 1 } })
  await act(async () => { await client.invalidateQueries({ queryKey: QK.pipelineJob('fetch-1') }) })
  await settle()
  expect(host.querySelector('[data-chart]')?.textContent).toBe('12')
  for (const key of related) expect(client.getQueryState(key)?.isInvalidated).toBe(true)
  for (const key of unrelated) expect(client.getQueryState(key)?.isInvalidated).toBe(false)
  expect(mocks.fetch).toHaveBeenCalledTimes(1)
})

it('SSE 在任务终态前送达分钟行并卸载按钮时，批量缓存仍完整回读一次', async () => {
  const other = '000001.SZ'
  const outside = '300001.SZ'
  const batchKey = QK.minuteBatch(`${symbol},${other},${outside}`)
  const rows = [{ datetime: `${date} 09:30:00`, close: 10 }, { datetime: `${date} 15:00:00`, close: 12 }]
  const outsideRows = [{ datetime: `${date} 15:00:00`, close: 20 }]
  client.setQueryData(batchKey, { data: { [other]: rows.slice(-1), [outside]: outsideRows } })
  await render()
  await click('采集该日分钟数据')
  expect(button('采集中…').disabled).toBe(true)
  await act(async () => client.setQueryData(QK.klineMinute(symbol, date), { symbol, date, source: 'local', rows }))
  await settle()
  expect(host.querySelector('[data-chart]')).not.toBeNull()
  expect(host.textContent).not.toContain('采集中')
  expect(client.getQueryData(QK.pipelineJob('fetch-1'))).toMatchObject({ status: 'running' })
  expect(client.getQueryData(batchKey)).toMatchObject({ fullReadSymbols: [symbol] })
  expect(client.getQueryState(batchKey)?.isInvalidated).toBe(true)
  mocks.batch.mockImplementation(async (_symbols, _date, _preferLocal, since) => ({
    data: { [symbol]: rows.filter(row => !since || row.datetime >= since), [other]: rows.slice(-1) },
    incremental: !!since,
  }))
  const result = await fetchMinuteBatchIncremental(client, batchKey, [symbol, other], true)
  expect(result.data[symbol]).toEqual(rows)
  expect(result.data[outside]).toEqual(outsideRows)
  expect(result.fullReadSymbols).toBeUndefined()
  client.setQueryData(batchKey, result)
  await act(async () => client.setQueryData(QK.klineMinute(symbol, date), { symbol, date, rows: [...rows, { datetime: `${date} 15:01:00`, close: 13 }] }))
  await settle()
  expect(client.getQueryState(batchKey)?.isInvalidated).toBe(false)
  expect(client.getQueryData(batchKey)).not.toHaveProperty('fullReadSymbols')
  await fetchMinuteBatchIncremental(client, batchKey, [symbol, other], true)
  expect(mocks.batch).toHaveBeenLastCalledWith([symbol, other], undefined, true, `${date} 15:00:00`)
  expect(mocks.fetch).toHaveBeenCalledTimes(1)
})

it('初次读取已有分钟数据不触发批量缓存全量回读', async () => {
  const batchKey = QK.minuteBatch(symbol)
  client.setQueryData(batchKey, { data: {} })
  mocks.minute.mockResolvedValueOnce({ symbol, date, rows: [{ close: 10 }] })
  await render()
  expect(host.querySelector('[data-chart]')).not.toBeNull()
  expect(client.getQueryData(batchKey)).not.toHaveProperty('fullReadSymbols')
  expect(client.getQueryState(batchKey)?.isInvalidated).toBe(false)
})

it.each([
  { nextSymbol: '000001.SZ', nextDate: date },
  { nextSymbol: symbol, nextDate: '2026-10-16' },
])('空态后切换到 $nextSymbol $nextDate 已有数据不误触发批量全量回读', async ({ nextSymbol, nextDate }) => {
  const batchKey = QK.minuteBatch(`${symbol},000001.SZ`)
  client.setQueryData(batchKey, { data: {} })
  await render()
  mocks.minute.mockResolvedValueOnce({ symbol: nextSymbol, date: nextDate, rows: [{ close: 10 }] })
  await render(nextSymbol, nextDate)
  expect(host.querySelector('[data-chart]')).not.toBeNull()
  expect(client.getQueryData(batchKey)).not.toHaveProperty('fullReadSymbols')
  expect(client.getQueryState(batchKey)?.isInvalidated).toBe(false)
})

it.each([
  { status: 'failed', error: '数据源请求失败', result: null, message: '数据源请求失败' },
  { status: 'succeeded', error: null, result: { rows: 0 }, message: '未获取到该日分钟数据' },
])('采集任务 $status 有明确反馈并允许重试', async result => {
  mocks.job.mockResolvedValue({ ...running, ...result })
  await render()
  await click('采集该日分钟数据')
  expect(host.textContent).toContain(result.message)
  expect(button('重新采集').disabled).toBe(false)
  mocks.fetch.mockResolvedValue({ status: 'started', symbol, date, job_id: 'retry-job', reused: false })
  mocks.job.mockResolvedValue({ ...running, id: 'retry-job' })
  await click('重新采集')
  expect(mocks.fetch).toHaveBeenCalledTimes(2)
  expect(host.textContent).toContain('40%')
})

it('提交失败显示原因，点击重试仍只采集原股票和日期', async () => {
  mocks.fetch.mockRejectedValueOnce(new Error('采集服务暂时不可用'))
  await render()
  await click('采集该日分钟数据')
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('采集服务暂时不可用')
  await click('重新采集')
  expect(mocks.fetch).toHaveBeenLastCalledWith(symbol, date)
  expect(host.textContent).toContain('40%')
})

it('任务进度读取失败可重试读取，不再次提交采集', async () => {
  mocks.job.mockRejectedValueOnce(new Error('网络中断'))
  await render()
  await click('采集该日分钟数据')
  expect(host.textContent).toContain('进度加载失败')
  await click('重试读取进度')
  expect(host.textContent).toContain('40%')
  expect(mocks.fetch).toHaveBeenCalledTimes(1)
})

it.each([
  { nextSymbol: '000001.SZ', nextDate: date },
  { nextSymbol: symbol, nextDate: '2026-10-16' },
])('切换为 $nextSymbol $nextDate 后丢弃旧提交响应', async ({ nextSymbol, nextDate }) => {
  let resolve!: (value: unknown) => void
  mocks.fetch.mockReturnValue(new Promise(done => { resolve = done }))
  await render()
  await click('采集该日分钟数据')
  await render(nextSymbol, nextDate)
  await act(async () => resolve({ status: 'started', symbol, date, job_id: 'old-job', reused: false }))
  await settle()
  expect(mocks.job).not.toHaveBeenCalled()
  expect(button('采集该日分钟数据').disabled).toBe(false)
  expect(host.textContent).not.toContain('40%')
  mocks.fetch.mockResolvedValue({ status: 'started', symbol: nextSymbol, date: nextDate, job_id: 'new-job', reused: false })
  await click('采集该日分钟数据')
  expect(mocks.fetch).toHaveBeenLastCalledWith(nextSymbol, nextDate)
})

it('切换股票后旧任务完成不污染新股票的状态或数据', async () => {
  await render()
  await click('采集该日分钟数据')
  await render('000001.SZ')
  await act(async () => client.setQueryData(QK.pipelineJob('fetch-1'), { ...running, status: 'succeeded', result: { rows: 1 } }))
  await settle()
  expect(host.querySelector('[data-chart]')).toBeNull()
  expect(host.textContent).toContain('后台尚未采集')
  expect(host.textContent).not.toContain('采集已完成')
  expect(button('采集该日分钟数据').disabled).toBe(false)
})

it('检查能力期间禁用采集，缺少能力给出数据源配置入口', async () => {
  let resolve!: (value: unknown) => void
  mocks.matrix.mockReturnValue(new Promise(done => { resolve = done }))
  await render()
  expect(button('采集该日分钟数据').disabled).toBe(true)
  expect(host.textContent).toContain('正在检查分钟采集能力')
  await act(async () => resolve({ capabilities: [{ id: 'minute', usable: false }] }))
  await settle()
  expect(button('采集该日分钟数据').disabled).toBe(true)
  expect(host.textContent).toContain('请先配置可用的分钟数据源')
  expect(button('分钟数据 · 不可用')).toBeDefined()
  expect(mocks.fetch).not.toHaveBeenCalled()
})

it('能力查询失败可重试检查，在结果就绪前不提交采集', async () => {
  mocks.matrix.mockRejectedValueOnce(new Error('能力读取失败'))
  await render()
  expect(host.textContent).toContain('无法读取分钟采集能力')
  expect(button('采集该日分钟数据').disabled).toBe(true)
  await click('重新检查')
  expect(button('采集该日分钟数据').disabled).toBe(false)
  expect(mocks.fetch).not.toHaveBeenCalled()
})

it('缺少采集能力仍显示已有分钟图，读取错误只提供重新加载', async () => {
  mocks.matrix.mockResolvedValue({ capabilities: [{ id: 'minute', usable: false }] })
  mocks.minute.mockResolvedValueOnce({ symbol, date, rows: [{ close: 10 }] })
  await render()
  expect(host.querySelector('[data-chart]')?.textContent).toBe('10')
  mocks.minute.mockRejectedValueOnce(new Error('分钟数据读取失败'))
  await render('000001.SZ')
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('分钟数据读取失败')
  expect(host.textContent).not.toContain('采集该日分钟数据')
  await click('重新加载')
  expect(host.textContent).toContain('请先配置可用的分钟数据源')
  expect(mocks.fetch).not.toHaveBeenCalled()
})
