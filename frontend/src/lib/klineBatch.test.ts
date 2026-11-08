// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { api } from './api'
import { sessionFetch } from './sessionRuntime'

vi.mock('./sessionRuntime', () => ({ sessionFetch: vi.fn() }))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
afterEach(() => { vi.resetAllMocks(); vi.useRealTimers() })

const symbols = Array.from({ length: 1001 }, (_, i) => `${String(i).padStart(6, '0')}.SZ`)

it('日K分块串行读取、去重并完整合并，保留调用方日期窗口', async () => {
  const sizes: number[] = []
  let inFlight = false
  vi.mocked(sessionFetch).mockImplementation(async (url, init) => {
    expect(inFlight).toBe(false)
    inFlight = true
    const body = JSON.parse(String(init!.body))
    expect(url).toBe('/api/kline/daily-batch')
    expect(body.days).toBe(35)
    sizes.push(body.symbols.length)
    await Promise.resolve()
    inFlight = false
    return new Response(JSON.stringify({ data: Object.fromEntries(body.symbols.map((symbol: string) => [symbol, [{ date: '2026-11-02', close: 10 }]])) }))
  })
  const result = await api.klineDailyBatch([...symbols, symbols[0]], 35)
  expect(sizes).toEqual([400, 400, 201])
  expect(Object.keys(result.data)).toEqual(symbols)
})

it('分钟分块保留同一日期和续传边界，任一块全量返回则不标记整批增量', async () => {
  const since = '2026-11-02T09:30:00'
  const requests: object[] = []
  vi.mocked(sessionFetch).mockImplementation(async (_url, init) => {
    const body = JSON.parse(String(init!.body))
    requests.push(body)
    expect(body).toMatchObject({ date: '2026-11-02', since, prefer_local: true })
    return new Response(JSON.stringify({ data: {}, full_minute_local: true, incremental: requests.length === 1 }))
  })
  expect(await api.klineMinuteBatch(symbols.slice(0, 401), '2026-11-02', true, since)).toEqual({
    data: {}, full_minute_local: true, incremental: false,
  })
  expect(requests).toHaveLength(2)
})

it('切换上下文取消后停止发出剩余块，块失败不返回部分成功结果', async () => {
  const controller = new AbortController()
  vi.mocked(sessionFetch).mockImplementationOnce(async () => {
    controller.abort()
    return new Response(JSON.stringify({ data: { [symbols[0]]: [] } }))
  })
  await expect(api.klineDailyBatch(symbols, 12, controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
  expect(sessionFetch).toHaveBeenCalledTimes(1)
  vi.mocked(sessionFetch).mockReset()
  vi.mocked(sessionFetch).mockResolvedValueOnce(new Response(JSON.stringify({ data: { [symbols[0]]: [] } })))
  vi.mocked(sessionFetch).mockRejectedValueOnce(new Error('读取失败'))
  await expect(api.klineDailyBatch(symbols)).rejects.toThrow('读取失败')
  expect(sessionFetch).toHaveBeenCalledTimes(2)
})

it('查询取消信号与每块超时同时生效，取消不会误报为超时', async () => {
  vi.useFakeTimers()
  vi.mocked(sessionFetch).mockImplementation((_url, init) => new Promise((_resolve, reject) => {
    init!.signal!.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })
  }))
  const controller = new AbortController()
  const timedOut = expect(api.klineDailyBatch(symbols, 12, controller.signal)).rejects.toThrow('请求超时（30s）')
  await vi.advanceTimersByTimeAsync(30_000)
  await timedOut
  expect(sessionFetch).toHaveBeenCalledTimes(1)

  const cancelled = expect(api.klineDailyBatch(symbols, 12, controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
  controller.abort()
  await cancelled
  expect(sessionFetch).toHaveBeenCalledTimes(2)
  expect(vi.getTimerCount()).toBe(0)
})
