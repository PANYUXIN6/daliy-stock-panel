// @vitest-environment jsdom
import { act, useState, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { Modal } from '@/components/Modal'
import { useCollectionSyncConfirmation } from './useCollectionSyncConfirmation'

const mocks = vi.hoisted(() => ({ check: vi.fn(), collect: vi.fn(), closeParent: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { collectionSyncCheck: mocks.check } }))

function Harness({ symbol = '600000.SH' }: { symbol?: string }) {
  const confirmation = useCollectionSyncConfirmation(symbol)
  return <>
    <button disabled={confirmation.busy} onClick={() => confirmation.run({ kind: 'minute', symbol, days: 5 }, () => mocks.collect(symbol, 5))}>获取数据</button>
    {confirmation.error && <p role="alert">{confirmation.error}</p>}
    {confirmation.dialog}
  </>
}

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  mocks.check.mockResolvedValue({ requires_confirmation: true, message: '相同范围已成功同步，数据源仍可能修订。', last_synced_at: '2026-10-11T08:00:00Z' })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
})
async function render(node: ReactNode) {
  await act(async () => root.render(node))
}
async function click(text: string) {
  const button = [...document.querySelectorAll('button')].find(node => node.textContent === text)!
  await act(async () => button.click())
}

it('重复采集先展示范围证据，取消不执行且允许重新操作', async () => {
  await render(<Harness />)
  await click('获取数据')
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain('相同范围已成功同步')
  expect(host.querySelector('button')!.disabled).toBe(true)
  expect(mocks.collect).not.toHaveBeenCalled()
  await click('取消')
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  expect(mocks.collect).not.toHaveBeenCalled()
  expect(host.querySelector('button')!.disabled).toBe(false)
})

it('确认仅调用一次原采集入口并保持检查参数', async () => {
  await render(<Harness />)
  await click('获取数据')
  const confirm = [...document.querySelectorAll('button')].find(node => node.textContent === '重新拉取')!
  await act(async () => { confirm.click(); confirm.click() })
  expect(mocks.check).toHaveBeenCalledWith({ kind: 'minute', symbol: '600000.SH', days: 5 })
  expect(mocks.collect).toHaveBeenCalledExactlyOnceWith('600000.SH', 5)
})

it('没有重复证据时直接走原同步入口，预检查失败时显示原因并停止', async () => {
  mocks.check.mockResolvedValueOnce({ requires_confirmation: false, message: null, last_synced_at: null })
  await render(<Harness />)
  await click('获取数据')
  expect(mocks.collect).toHaveBeenCalledOnce()
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  mocks.check.mockRejectedValueOnce(new Error('本地同步记录读取失败'))
  await click('获取数据')
  expect(host.querySelector('[role="alert"]')?.textContent).toBe('本地同步记录读取失败')
  expect(mocks.collect).toHaveBeenCalledOnce()
})

it('预检查期间重复点击不发起第二次检查', async () => {
  let resolve: (result: unknown) => void = () => {}
  mocks.check.mockImplementationOnce(() => new Promise(done => { resolve = done }))
  await render(<Harness />)
  const trigger = host.querySelector('button')!
  await act(async () => { trigger.click(); trigger.click() })
  expect(mocks.check).toHaveBeenCalledOnce()
  await act(async () => resolve({ requires_confirmation: false }))
  expect(mocks.collect).toHaveBeenCalledOnce()
})

it('切换标的后丢弃尚未返回的旧检查，避免提交过时标的', async () => {
  let resolve: (result: unknown) => void = () => {}
  mocks.check.mockImplementationOnce(() => new Promise(done => { resolve = done }))
  await render(<Harness />)
  await click('获取数据')
  await render(<Harness symbol="600519.SH" />)
  await act(async () => resolve({ requires_confirmation: false }))
  expect(mocks.collect).not.toHaveBeenCalled()
  await click('获取数据')
  await click('重新拉取')
  expect(mocks.collect).toHaveBeenCalledExactlyOnceWith('600519.SH', 5)
})

it('待确认时切换上下文关闭旧对话框', async () => {
  await render(<Harness />)
  await click('获取数据')
  await render(<Harness symbol="600519.SH" />)
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  expect(mocks.collect).not.toHaveBeenCalled()
})

it('嵌套弹窗 Escape 只取消重复确认，关闭后恢复到原采集按钮', async () => {
  function Nested() {
    const [open, setOpen] = useState(true)
    return open ? <Modal ariaLabel="采集配置" onClose={() => { mocks.closeParent(); setOpen(false) }}><Harness /></Modal> : null
  }
  await render(<Nested />)
  await act(async () => { await new Promise(done => setTimeout(done, 20)) })
  const trigger = host.querySelector('button')!
  trigger.focus()
  await click('获取数据')
  await act(async () => { await new Promise(done => setTimeout(done, 20)) })
  expect(document.activeElement?.textContent).toBe('取消')
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(mocks.closeParent).not.toHaveBeenCalled()
  expect(document.querySelectorAll('[role="dialog"]')).toHaveLength(1)
  expect(document.activeElement).toBe(trigger)
  expect(mocks.collect).not.toHaveBeenCalled()
})
