// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { api } from './api'
import { startReviewGeneration, cancelReviewGeneration, getReviewState, resetReview } from './reviewStore'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
afterEach(() => { cancelReviewGeneration(); resetReview(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

it('显式取消中止 fetch，在没有新事件时收尾并释放响应 reader', async () => {
  let controller: ReadableStreamDefaultController<Uint8Array>
  const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value } })
  const readerCancel = vi.spyOn(ReadableStreamDefaultReader.prototype, 'cancel')
  const readerRelease = vi.spyOn(ReadableStreamDefaultReader.prototype, 'releaseLock')
  const fetchMock = vi.fn(async (_url: string, options?: RequestInit) => {
    options?.signal?.addEventListener('abort', () => controller.error(new DOMException('Aborted', 'AbortError')))
    return new Response(stream, { status: 200 })
  })
  vi.stubGlobal('fetch', fetchMock)
  const onDone = vi.fn()
  const generation = startReviewGeneration('2026-10-12', '', onDone)
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled())
  controller!.enqueue(new TextEncoder().encode('{"type":"delta","content":"部分内容"}\n'))
  await vi.waitFor(() => expect(getReviewState().phase).toBe('streaming'))
  const signal = fetchMock.mock.calls[0][1]?.signal
  expect(signal).toBeInstanceOf(AbortSignal)
  cancelReviewGeneration()
  await generation
  expect(signal?.aborted).toBe(true)
  expect(getReviewState().phase).toBe('idle')
  expect(onDone).not.toHaveBeenCalled()
  expect(readerCancel).toHaveBeenCalledOnce()
  expect(readerRelease).toHaveBeenCalledOnce()
  expect(stream.locked).toBe(false)
})

it('提前退出复盘 generator 时取消响应流并解锁', async () => {
  const cancel = vi.fn()
  const stream = new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(new TextEncoder().encode('{"type":"delta","content":"部分内容"}\n')) },
    cancel,
  })
  vi.stubGlobal('fetch', vi.fn(async () => new Response(stream, { status: 200 })))
  for await (const _event of api.reviewStream('2026-10-12', '')) break
  expect(cancel).toHaveBeenCalledOnce()
  expect(stream.locked).toBe(false)
})

it('旧请求取消后的收尾不清理新复盘 controller 或覆盖新状态', async () => {
  let rejectOld: (reason: Error) => void = () => {}
  let rejectNew: (reason: Error) => void = () => {}
  const fetchMock = vi.fn()
    .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectOld = reject }))
    .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectNew = reject }))
  vi.stubGlobal('fetch', fetchMock)
  const oldGeneration = startReviewGeneration('2026-10-11', '旧请求')
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledOnce())
  cancelReviewGeneration()
  const newGeneration = startReviewGeneration('2026-10-12', '新请求')
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
  rejectOld(new DOMException('Aborted', 'AbortError'))
  await oldGeneration
  expect(getReviewState()).toMatchObject({ phase: 'loading', focus: '新请求' })
  cancelReviewGeneration()
  expect((fetchMock.mock.calls[1][1] as RequestInit).signal?.aborted).toBe(true)
  rejectNew(new DOMException('Aborted', 'AbortError'))
  await newGeneration
  expect(getReviewState().phase).toBe('idle')
})


it('手动复盘无 done 的 EOF 保留残文但不伪造成功归档', async () => {
  const stream = new ReadableStream<Uint8Array>({ start(controller) {
    controller.enqueue(new TextEncoder().encode('{"type":"delta","content":"未完成的复盘"}\n'))
    controller.close()
  } })
  vi.stubGlobal('fetch', vi.fn(async () => new Response(stream, { status: 200 })))
  const onDone = vi.fn()
  await startReviewGeneration('2026-10-12', '', onDone)
  expect(getReviewState()).toMatchObject({ phase: 'error', content: '未完成的复盘', error: expect.stringContaining('中断') })
  expect(onDone).not.toHaveBeenCalled()
  expect(stream.locked).toBe(false)
})


it('复盘正常 done 收尾一次完成回调且释放 reader', async () => {
  const stream = new ReadableStream<Uint8Array>({ start(controller) {
    controller.enqueue(new TextEncoder().encode('{"type":"meta","as_of":"2026-10-12","archived":true}\n{"type":"delta","content":"已完成复盘"}\n{"type":"done","archived":true}\n'))
    controller.close()
  } })
  vi.stubGlobal('fetch', vi.fn(async () => new Response(stream, { status: 200 })))
  const onDone = vi.fn()
  await startReviewGeneration('2026-10-12', '', onDone)
  expect(getReviewState()).toMatchObject({ phase: 'done', content: '已完成复盘', error: '' })
  expect(onDone).toHaveBeenCalledExactlyOnceWith('已完成复盘', expect.objectContaining({ as_of: '2026-10-12', archived: true }))
  expect(stream.locked).toBe(false)
})
