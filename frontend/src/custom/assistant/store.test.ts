// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest'
import type { AssistantEvent, ChatHistoryMessage } from './client'

const fixtures = vi.hoisted(() => ({
  replies: [] as AssistantEvent[][],
  requests: [] as { messages: ChatHistoryMessage[] }[],
}))
vi.mock('./client', () => ({
  assistantChatStream: async function* (body: { messages: ChatHistoryMessage[] }) {
    fixtures.requests.push(structuredClone(body))
    for (const event of fixtures.replies.shift() ?? [{ type: 'done' }]) yield event
  },
  decideAssistantAction: async () => undefined,
}))

beforeEach(() => {
  vi.resetModules()
  localStorage.clear()
  fixtures.replies.length = 0
  fixtures.requests.length = 0
})

async function load(owner = 'owner-a') {
  const runtime = await import('@/lib/sessionRuntime')
  runtime.activateSession(owner)
  const store = await import('./store')
  return { ...store, storage: runtime.accountStorage }
}

async function waitFinished(store: Awaited<ReturnType<typeof load>>) {
  await vi.waitFor(() => expect(store.activeSession()?.messages.some(m => m.role === 'assistant' && m.streaming)).toBe(false))
}

it.each(['模型推理', ''])('preserves final reasoning after reload and forwards it in the next question (%s)', async reasoning => {
  fixtures.replies.push([
    { type: 'tool_call', call_id: 'one', name: 'list_strategies', args: {} },
    { type: 'tool_result', call_id: 'one', name: 'list_strategies', ok: true, summary: '完成', elapsed_ms: 1 },
    { type: 'delta', content: '最终回答' },
    { type: 'assistant_context', reasoning_content: reasoning, reasoning_context: 'a'.repeat(64) },
    { type: 'done' },
  ])
  let store = await load()
  store.sendMessage('查询策略')
  await waitFinished(store)
  const saved = JSON.parse(store.storage.getItem('assistant.sessions.v1')!)
  const answer = saved.sessions[0].messages.find((m: { role: string }) => m.role === 'assistant')
  expect(answer.content).toBe('最终回答')
  expect(answer.reasoningContent).toBe(reasoning)
  expect(answer.reasoningContext).toBe('a'.repeat(64))
  expect(localStorage.getItem('assistant.sessions.v1')).toBeNull()

  vi.resetModules()
  store = await load()
  store.sendMessage('继续解释')
  await waitFinished(store)
  expect(fixtures.requests[1].messages).toEqual([
    { role: 'user', content: '查询策略' },
    { role: 'assistant', content: '最终回答', reasoning_content: reasoning, reasoning_context: 'a'.repeat(64) },
    { role: 'user', content: '继续解释' },
  ])
})

it('loads old text-only sessions without inventing reasoning metadata', async () => {
  const runtime = await import('@/lib/sessionRuntime')
  runtime.activateSession('owner-a')
  runtime.accountStorage.setItem('assistant.sessions.v1', JSON.stringify({ activeId: 'old', sessions: [{
    id: 'old', title: '旧会话', createdAt: 1, messages: [
      { id: 'u', role: 'user', content: '旧问题', ts: 1 },
      { id: 'a', role: 'assistant', content: '旧回答', ts: 2, streaming: false },
    ],
  }] }))
  const store = await load()
  store.sendMessage('继续')
  await waitFinished(store)
  expect(fixtures.requests[0].messages).toEqual([
    { role: 'user', content: '旧问题' }, { role: 'assistant', content: '旧回答' },
    { role: 'user', content: '继续' },
  ])
})

it('keeps saved reasoning inside its existing account partition', async () => {
  fixtures.replies.push([
    { type: 'delta', content: 'A的回答' },
    { type: 'assistant_context', reasoning_content: 'A的推理', reasoning_context: 'a'.repeat(64) },
    { type: 'done' },
  ])
  const a = await load('owner-a')
  a.sendMessage('A的问题')
  await waitFinished(a)
  vi.resetModules()
  const b = await load('owner-b')
  expect(b.activeSession()).toBeNull()
  b.sendMessage('B的问题')
  await waitFinished(b)
  expect(fixtures.requests[1].messages).toEqual([{ role: 'user', content: 'B的问题' }])
  expect(localStorage.getItem('tf-user:owner-a:assistant.sessions.v1')).toContain('A的推理')
  expect(localStorage.getItem('tf-user:owner-b:assistant.sessions.v1')).not.toContain('A的推理')
})
