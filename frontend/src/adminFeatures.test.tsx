// @vitest-environment jsdom
import { act, Suspense } from 'react'
import { createRoot } from 'react-dom/client'
import { RouterProvider, Outlet } from 'react-router-dom'
import { expect, it, vi } from 'vitest'
import { AuthContext } from './lib/auth'
import type { User } from './lib/api'

vi.mock('./components/Layout', () => ({ Layout: () => <Suspense fallback="加载"><Outlet /></Suspense> }))
vi.mock('./pages/Backtest', () => ({ Backtest: () => <div>回测内容</div> }))
vi.mock('./pages/Monitor', () => ({ Monitor: () => <div>监控内容</div> }))
vi.mock('./pages/Signals', () => ({ Signals: () => <div>信号内容</div> }))
vi.mock('./pages/Lots', () => ({ Lots: () => <div>持仓提醒内容</div> }))

it.each(['guest', 'pro', 'admin'] as const)('checks direct feature URLs for %s', async role => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const { router } = await import('./router')
  const host = document.createElement('div')
  document.body.append(host)
  const root = createRoot(host)
  const user = { id: 'test', role } as User
  try {
    for (const [path, content] of [['/backtest', '回测内容'], ['/monitor', '监控内容'], ['/signals', '信号内容'], ['/lots', '持仓提醒内容']]) {
      await act(async () => {
        await router.navigate(path)
        root.render(<AuthContext.Provider value={user}><RouterProvider router={router} /></AuthContext.Provider>)
      })
      if (role === 'admin') expect(host.textContent).toContain(content)
      else {
        expect(host.textContent).toContain('仅管理员')
        expect(host.textContent).not.toContain(content)
      }
    }
  } finally {
    await act(async () => root.unmount())
    host.remove()
  }
})
