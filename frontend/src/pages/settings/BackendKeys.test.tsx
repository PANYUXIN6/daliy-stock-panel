// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, expect, it, vi } from 'vitest'
import { SettingsAIPanel } from './AI'
import { TickFlowKeySection } from './Keys'

const mocks = vi.hoisted(() => ({
  settings: {
    data: {
      has_tickflow_key: true,
      has_ai_key: true,
      ai_configured: true,
      ai_provider: 'openai_compat',
      ai_base_url: 'https://api.deepseek.com',
      ai_model: 'deepseek-flash',
      tickflow_api_key_masked: 'legacy-tickflow-fragment',
      ai_api_key_masked: 'legacy-ai-fragment',
    },
    isLoading: false,
    isError: false,
  },
  save: vi.fn(),
  test: vi.fn(),
  redetect: vi.fn(),
}))

vi.mock('@/lib/useSharedQueries', () => ({ useSettings: () => mocks.settings }))
vi.mock('@/lib/api', () => ({ api: {
  saveAiSettings: mocks.save,
  strategyAiTest: mocks.test,
  redetectCapabilities: mocks.redetect,
} }))

beforeEach(() => {
  vi.clearAllMocks()
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.settings.data.has_tickflow_key = true
  mocks.settings.data.has_ai_key = true
  mocks.settings.data.ai_configured = true
  mocks.save.mockResolvedValue({ ok: true, ai_configured: true })
  mocks.test.mockResolvedValue({ ok: true, model: 'deepseek-flash' })
})

async function renderPanels() {
  const host = document.createElement('div')
  const root = createRoot(host)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root.render(
    <QueryClientProvider client={qc}>
      <TickFlowKeySection />
      <SettingsAIPanel />
    </QueryClientProvider>,
  ))
  return { host, cleanup: async () => { await act(async () => root.unmount()); qc.clear() } }
}

it('shows backend status without displaying legacy key fragments or credential controls', async () => {
  const { host, cleanup } = await renderPanels()
  try {
    expect(host.textContent).toContain('后端已配置')
    expect(host.textContent).toContain('deepseek-flash')
    expect(host.textContent).not.toContain('legacy-tickflow-fragment')
    expect(host.textContent).not.toContain('legacy-ai-fragment')
    expect(host.querySelector('input[type="password"]')).toBeNull()
    expect([...host.querySelectorAll('button')].some(b => /清空|清除/.test(b.textContent ?? ''))).toBe(false)
    const save = [...host.querySelectorAll('button')].find(b => b.textContent === '保存配置')!
    await act(async () => save.click())
    expect(mocks.save).toHaveBeenCalledWith(expect.objectContaining({ model: 'deepseek-flash' }))
    expect(mocks.save.mock.calls[0][0]).not.toHaveProperty('api_key')
    const test = [...host.querySelectorAll('button')].find(b => b.textContent === '测试')!
    await act(async () => test.click())
    expect(mocks.test).toHaveBeenCalledOnce()
    expect(host.textContent).toContain('连通成功')
  } finally { await cleanup() }
})

it('directs unconfigured users to backend configuration without a key input', async () => {
  mocks.settings.data.has_tickflow_key = false
  mocks.settings.data.has_ai_key = false
  mocks.settings.data.ai_configured = false
  const { host, cleanup } = await renderPanels()
  try {
    expect(host.textContent).toContain('后端未配置')
    expect(host.textContent).toContain('TICKFLOW_API_KEY')
    expect(host.textContent).toContain('AI_API_KEY')
    expect(host.querySelector('input[type="password"]')).toBeNull()
  } finally { await cleanup() }
})
