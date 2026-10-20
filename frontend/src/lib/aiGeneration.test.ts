// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from './api'
import { assistantChatStream } from '@/custom/assistant/client'
import { activateSession } from './sessionRuntime'
import { creditsFixture } from '@/test/aiCredits'
import { rememberAiCreditPreview, runAiGeneration, subscribeAiCredits } from './aiGeneration'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
beforeEach(() => { activateSession('guest-test'); rememberAiCreditPreview(creditsFixture()) })
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

async function consume(stream: AsyncGenerator<unknown>) { for await (const _event of stream) { /* 读取完整任务 */ } }

it('六个生成入口都先读取规则并携带独立任务标识和服务端版本', async () => {
  const requests: { url: string; headers: Headers }[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url === '/api/ai/credits') return Response.json(creditsFixture())
    requests.push({ url, headers: new Headers(init?.headers) })
    return new Response('{"type":"delta","content":"结果"}\n{"type":"done"}\n')
  }))
  await consume(assistantChatStream({ messages: [{ role: 'user', content: '市场如何' }] }))
  await consume(api.stockAnalyzeStream('600000.SH'))
  await consume(api.financialAnalyzeStream('600000.SH'))
  await consume(api.reviewStream('2026-10-20'))
  await consume(api.rotationAnalyzeStream(12, '', 'concept'))
  await consume(api.rotationAnalyzeStream(12, '', 'industry', 2))
  expect(requests.map(request => request.url)).toEqual([
    '/api/custom/assistant/chat', '/api/stock-analysis/analyze', '/api/financials/analyze',
    '/api/market-recap/analyze', '/api/rps/rotation-analyze', '/api/rps/rotation-analyze',
  ])
  const ids = requests.map(request => request.headers.get('X-AI-Request-ID'))
  expect(new Set(ids).size).toBe(6)
  for (const request of requests) {
    expect(request.headers.get('X-AI-Policy-Version')).toBe('7')
    expect(request.headers.get('X-Expected-User')).toBe('guest-test')
    expect(request.headers.get('X-AI-Request-ID')).toMatch(/^[\da-f-]{36}$/)
  }
})

it.each([
  ['assistant', '本月点数不足'],
  ['industry_rotation', '本月 AI 点数不足'],
] as const)('服务器拒绝 %s 时不发起生成且通知刷新余额', async (feature, reason) => {
  const credits = creditsFixture()
  credits.features[feature] = { ...credits.features[feature], available: false, reason }
  const fetch = vi.fn(async () => Response.json(credits))
  vi.stubGlobal('fetch', fetch)
  const generate = vi.fn(async function* () { yield { type: 'done' } })
  const changed = vi.fn()
  const stop = subscribeAiCredits(changed)
  try {
    await expect(consume(runAiGeneration(feature, generate))).rejects.toThrow(reason)
    expect(generate).not.toHaveBeenCalled()
    expect(fetch).toHaveBeenCalledOnce()
    expect(changed.mock.calls.at(-1)?.[0]).toEqual({ type: 'refresh' })
  } finally { stop() }
})

it('策略版本在预检后变化时显示后端错误且不自动重试生成', async () => {
  const fetch = vi.fn(async (url: string) => url === '/api/ai/credits'
    ? Response.json(creditsFixture())
    : Response.json({ detail: 'AI 点数规则已更新，请重新发起', code: 'AI_POLICY_CHANGED' }, { status: 409 }))
  vi.stubGlobal('fetch', fetch)
  await expect(consume(api.stockAnalyzeStream('600000.SH'))).rejects.toThrow('AI 点数规则已更新')
  expect(fetch.mock.calls.filter(([url]) => url === '/api/stock-analysis/analyze')).toHaveLength(1)
})

it('所有入口采用各自的服务端权限，不由报告共用价格推断可用性', async () => {
  const credits = creditsFixture()
  credits.features.concept_rotation.available = false
  credits.features.concept_rotation.reason = '概念分析已暂停'
  const generated: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url === '/api/ai/credits') return Response.json(credits)
    generated.push(JSON.parse(init?.body as string).kind)
    return new Response('{"type":"done"}\n')
  }))
  await expect(consume(api.rotationAnalyzeStream(12, '', 'concept'))).rejects.toThrow('概念分析已暂停')
  await consume(api.rotationAnalyzeStream(12, '', 'industry', 2))
  expect(generated).toEqual(['industry'])
})

it('读取额度期间取消助手，不会在预检返回后继续发起付费请求', async () => {
  const fetch = vi.fn((_url: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
    init?.signal?.addEventListener('abort', () => reject(new DOMException('已取消', 'AbortError')))
  }))
  vi.stubGlobal('fetch', fetch)
  const controller = new AbortController()
  const pending = consume(assistantChatStream({ messages: [{ role: 'user', content: '问题' }] }, controller.signal))
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce())
  controller.abort()
  await expect(pending).rejects.toThrow('已取消')
  expect(fetch).toHaveBeenCalledOnce()
  expect(fetch.mock.calls[0][0]).toBe('/api/ai/credits')
})

it('界面展示后规则涨价时先提示重新确认，确认新价格后才能生成', async () => {
  const latest = creditsFixture({ policy_version: 8 })
  latest.features.assistant.points = 5
  vi.stubGlobal('fetch', vi.fn(async () => Response.json(latest)))
  const generate = vi.fn(async function* () { yield { type: 'done' } })
  await expect(consume(runAiGeneration('assistant', generate))).rejects.toThrow('本次未扣点')
  expect(generate).not.toHaveBeenCalled()
  rememberAiCreditPreview(latest)
  await consume(runAiGeneration('assistant', generate))
  expect(generate).toHaveBeenCalledOnce()
})
