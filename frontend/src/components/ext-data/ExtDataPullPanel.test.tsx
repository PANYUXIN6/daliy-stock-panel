// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import type { ExtDataConfig } from '@/lib/api'
import { ExtDataPullPanel } from './ExtDataPullPanel'

const mocks = vi.hoisted(() => ({ check: vi.fn(), run: vi.fn(), saved: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: {
  collectionSyncCheck: mocks.check, extDataPullRun: mocks.run,
  extDataApiKey: async () => ({ key_set: false, masked_key: '' }),
} }))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

const config: ExtDataConfig = {
  id: 'ext_sample', label: '测试数据', mode: 'snapshot', fields: [], created_at: '', updated_at: '',
  pull: { url: 'https://example.test/data', method: 'GET', response_path: '', schedule_minutes: 1440, enabled: false, timeout_seconds: 45 },
}
let host: HTMLDivElement
let root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  mocks.check.mockResolvedValue({ requires_confirmation: true, message: '本地已有当前批次数据，是否重新拉取？', last_synced_at: null })
  mocks.run.mockResolvedValue({ rows: 12, date: '2026-10-11' })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => { await act(async () => root.unmount()); host.remove() })
async function click(text: string) {
  const button = [...document.querySelectorAll('button')].find(node => node.textContent?.trim() === text)!
  await act(async () => button.click())
}

it('通用扩展重复采集取消无请求，确认保持配置和超时参数', async () => {
  await act(async () => root.render(<ExtDataPullPanel config={config} onSaved={mocks.saved} />))
  await click('立即执行')
  expect(mocks.check).toHaveBeenCalledWith({ kind: 'ext', config_id: 'ext_sample' })
  await click('取消')
  expect(mocks.run).not.toHaveBeenCalled()
  await click('立即执行')
  await click('重新拉取')
  expect(mocks.run).toHaveBeenCalledExactlyOnceWith('ext_sample', 45)
  expect(mocks.saved).toHaveBeenCalledOnce()
  expect(host.textContent).toContain('12')
})

it('扩展配置在检查期间切换后不提交旧配置，错误也不会绕过检查', async () => {
  let resolve: (result: unknown) => void = () => {}
  mocks.check.mockImplementationOnce(() => new Promise(done => { resolve = done }))
  await act(async () => root.render(<ExtDataPullPanel config={config} onSaved={mocks.saved} />))
  await click('立即执行')
  await act(async () => root.render(<ExtDataPullPanel config={{ ...config, id: 'ext_changed' }} onSaved={mocks.saved} />))
  await act(async () => resolve({ requires_confirmation: false }))
  expect(mocks.run).not.toHaveBeenCalled()
  mocks.check.mockRejectedValueOnce(new Error('扩展本地数据不可读取'))
  await click('立即执行')
  expect(host.querySelector('[role="alert"]')?.textContent).toBe('扩展本地数据不可读取')
  expect(mocks.run).not.toHaveBeenCalled()
})
