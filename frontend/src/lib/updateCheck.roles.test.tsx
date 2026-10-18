// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { activateSession } from './sessionRuntime'
import { useUpdateCheck } from './updateCheck'

const identity = vi.hoisted(() => ({ role: 'guest' }))
vi.mock('@/lib/auth', () => ({ useCurrentUser: () => identity }))
vi.mock('@/lib/useSharedQueries', () => ({ useVersion: () => ({ data: { version: '0.3.7' } }) }))
let root: ReturnType<typeof createRoot>
let host: HTMLDivElement
let fetchMock: ReturnType<typeof vi.fn>
function Consumer() {
  const update = useUpdateCheck()
  return <button onClick={update.check}>{update.status}:{update.info?.latest}</button>
}
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers()
  localStorage.clear()
  activateSession('update-role-test')
  identity.role = 'guest'
  fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ tag_name: 'v0.3.8' }) }))
  vi.stubGlobal('fetch', fetchMock)
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})
it('guests do not check manually or periodically, and cannot see an admin result after role changes', async () => {
  await act(async () => root.render(<Consumer />))
  await act(async () => host.querySelector('button')!.click())
  await act(async () => vi.advanceTimersByTimeAsync(3600000))
  expect(fetchMock).not.toHaveBeenCalled()
  identity.role = 'admin'
  await act(async () => root.render(<Consumer />))
  expect(host.textContent).toBe('found:v0.3.8')
  expect(fetchMock).toHaveBeenCalledOnce()
  identity.role = 'guest'
  await act(async () => root.render(<Consumer />))
  expect(host.textContent).toBe('idle:')
  await act(async () => host.querySelector('button')!.click())
  await act(async () => vi.advanceTimersByTimeAsync(3600000))
  expect(fetchMock).toHaveBeenCalledOnce()
})
it('admin consumers share a timer, keep it while mounted, and restore it after remount', async () => {
  identity.role = 'admin'
  await act(async () => root.render(<><Consumer key="layout" /><Consumer key="settings" /></>))
  expect(fetchMock).toHaveBeenCalledOnce()
  await act(async () => root.render(<><Consumer key="layout" /></>))
  await act(async () => vi.advanceTimersByTimeAsync(3600000))
  expect(fetchMock).toHaveBeenCalledTimes(2)
  await act(async () => root.render(null))
  await act(async () => vi.advanceTimersByTimeAsync(3600000))
  expect(fetchMock).toHaveBeenCalledTimes(2)
  await act(async () => root.render(<Consumer />))
  await act(async () => vi.advanceTimersByTimeAsync(3600000))
  expect(fetchMock).toHaveBeenCalledTimes(3)
})
