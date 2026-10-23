// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ReviewPromptDialog } from './ReviewPromptDialog'

const mocks = vi.hoisted(() => ({ read: vi.fn(), save: vi.fn(), toast: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { reviewPrompt: mocks.read, updateReviewPrompt: mocks.save } }))
vi.mock('@/components/Toast', () => ({ toast: mocks.toast }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
const config = { custom: '', default: '默认复盘模板', customized: false, max_chars: 32_000 }
let cleanup = async () => {}
beforeEach(() => {
  vi.clearAllMocks()
  mocks.read.mockResolvedValue(config)
  mocks.save.mockImplementation(async (text: string) => ({ ...config, custom: text, customized: !!text }))
})
afterEach(async () => { await cleanup() })

async function settle() {
  for (let i = 0; i < 5; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

async function mount(userId = 'pro-first') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const host = document.createElement('div')
  document.body.append(host)
  const root = createRoot(host)
  const close = vi.fn()
  const update = async (id: string) => {
    await act(async () => root.render(<QueryClientProvider client={qc}>
      <ReviewPromptDialog key={id} userId={id} onClose={close} />
    </QueryClientProvider>))
    await settle()
  }
  cleanup = async () => { await act(async () => root.unmount()); qc.clear(); host.remove() }
  await update(userId)
  const button = (text: string) => [...host.querySelectorAll('button')].find(node => node.textContent === text)!
  const fill = async (value: string) => {
    await act(async () => {
      const textarea = host.querySelector('textarea')!
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(textarea, value)
      textarea.dispatchEvent(new Event('input', { bubbles: true }))
    })
  }
  return { host, close, update, button, fill }
}

it('loads the owner template, fills defaults, saves and restores defaults', async () => {
  const view = await mount()
  expect(view.host.textContent).toContain('使用默认模板')
  await act(async () => view.button('填入默认模板').click())
  expect(view.host.querySelector('textarea')!.value).toBe(config.default)
  await view.fill('只关注市场宽度和量能')
  await act(async () => view.button('保存提示词').click()); await settle()
  expect(mocks.save).toHaveBeenLastCalledWith('只关注市场宽度和量能', expect.anything())
  expect(view.close).toHaveBeenCalledOnce()
  await act(async () => view.button('恢复默认').click()); await settle()
  expect(mocks.save).toHaveBeenLastCalledWith('', expect.anything())
})

it('preserves the draft after a save failure and allows retry', async () => {
  mocks.save.mockRejectedValueOnce(new Error('temporary failure'))
  const view = await mount()
  await view.fill('保留草稿')
  await act(async () => view.button('保存提示词').click()); await settle()
  expect(view.host.textContent).toContain('草稿已保留')
  expect(view.host.querySelector('textarea')!.value).toBe('保留草稿')
  expect(view.close).not.toHaveBeenCalled()
  await act(async () => view.button('保存提示词').click()); await settle()
  expect(view.close).toHaveBeenCalledOnce()
})

it('offers retry after read failure and prevents saving before data is loaded', async () => {
  mocks.read.mockRejectedValueOnce(new Error('temporary failure'))
  const view = await mount()
  expect(view.host.textContent).toContain('提示词读取失败')
  expect(view.button('保存提示词').disabled).toBe(true)
  await act(async () => view.button('重试').click()); await settle()
  expect(view.host.querySelector('textarea')).not.toBeNull()
  expect(view.button('保存提示词').disabled).toBe(false)
})

it('enforces the character limit and clears drafts when switching accounts', async () => {
  mocks.read.mockResolvedValueOnce({ ...config, max_chars: 3 })
  const view = await mount()
  await view.fill('😀😀😀')
  expect(view.button('保存提示词').disabled).toBe(false)
  await view.fill('😀😀😀😀')
  expect(view.button('保存提示词').disabled).toBe(true)
  mocks.read.mockResolvedValueOnce({ ...config, custom: '另一个账号的模板', customized: true })
  await view.update('pro-second')
  expect(view.host.querySelector('textarea')!.value).toBe('另一个账号的模板')
  expect(mocks.save).not.toHaveBeenCalled()
})
