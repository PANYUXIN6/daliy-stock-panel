// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, it, vi } from 'vitest'
import { AuthContext } from '@/lib/auth'
import type { User } from '@/lib/api'
import { SettingsMenuSettingsPanel } from './MenuSettings'

vi.mock('@/lib/api', () => ({ api: { analysisMenus: async () => ({ items: [] }) } }))
vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: { nav_order: ['/backtest', '/monitor', '/signals', '/lots', '/watchlist'], nav_hidden: [] } }),
}))

it.each(['guest', 'pro', 'admin'] as const)('saved menu order respects %s permissions', async role => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const host = document.createElement('div')
  document.body.append(host)
  const root = createRoot(host)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  try {
    await act(async () => root.render(
      <AuthContext.Provider value={{ id: 'test', role } as User}>
        <QueryClientProvider client={client}><MemoryRouter><SettingsMenuSettingsPanel /></MemoryRouter></QueryClientProvider>
      </AuthContext.Provider>,
    ))
    const links = [...host.querySelectorAll('a')].map(a => a.getAttribute('href'))
    for (const path of ['/backtest', '/monitor', '/signals', '/lots']) expect(links.includes(path)).toBe(role === 'admin')
    expect(links).toContain('/watchlist')
  } finally {
    await act(async () => root.unmount())
    client.clear()
    host.remove()
  }
})
