// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { describe, expect, it, vi } from 'vitest'
import { DashboardGrid } from './DashboardGrid'
import type { WidgetCtx } from './registry'

const page = vi.hoisted(() => ({ scale: 1 }))
vi.mock('@/lib/pageZoom', () => ({ getPageScale: () => page.scale }))
vi.mock('react-grid-layout', async importOriginal => ({
  ...await importOriginal<typeof import('react-grid-layout')>(),
  useContainerWidth: () => ({ width: 1200, mounted: true, containerRef: { current: null } }),
}))
vi.mock('./registry', () => ({
  DEFAULT_LAYOUT: [],
  isKnownWidgetType: () => true,
  minSizeOf: () => ({ w: 1, h: 1 }),
  widgetDef: () => ({ label: '测试组件', render: () => <div>行情</div> }),
}))

describe('DashboardGrid drag coordinates', () => {
  it.each([
    { scale: 1, parentTop: 160, scrollLeft: 0, scrollTop: 0 },
    { scale: 1, parentTop: -120, scrollLeft: 20, scrollTop: 80 },
    { scale: 0.8, parentTop: -120, scrollLeft: 20, scrollTop: 80 },
    { scale: 1.5, parentTop: 160, scrollLeft: 20, scrollTop: 80 },
  ])('saves the intended column with $scale scale and top $parentTop', async ({ scale, parentTop, scrollLeft, scrollTop }) => {
    Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
    page.scale = scale
    const host = document.createElement('div')
    document.body.append(host)
    const root = createRoot(host)
    const onItemsChange = vi.fn()
    try {
      await act(async () => root.render(<DashboardGrid
        ctx={{} as WidgetCtx}
        items={[{ i: 'indices', t: 'indices', x: 1, y: 0, w: 2, h: 2 }]}
        editing
        onItemsChange={onItemsChange}
      />))
      const grid = host.querySelector('.react-grid-layout') as HTMLDivElement
      const card = grid.querySelector('.react-grid-item') as HTMLDivElement
      const parentLeft = 240
      const columnStep = (1200 - 6) / 12
      const cardLeft = parentLeft + (Math.round(6 + columnStep) - scrollLeft) * scale
      const cardTop = parentTop + (6 - scrollTop) * scale
      grid.scrollLeft = scrollLeft
      grid.scrollTop = scrollTop
      grid.getBoundingClientRect = () => ({ left: parentLeft, top: parentTop }) as DOMRect
      card.getBoundingClientRect = () => ({ left: cardLeft, top: cardTop }) as DOMRect
      Object.defineProperty(card, 'offsetParent', { configurable: true, value: grid })
      const start = { clientX: cardLeft + 30 * scale, clientY: cardTop + 20 * scale }
      const end = { clientX: start.clientX + 2 * columnStep * scale, clientY: start.clientY }

      const initialTransform = card.style.transform
      await act(async () => card.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, buttons: 1, ...start })))
      expect(card.style.transform).toBe(initialTransform)
      await act(async () => document.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, buttons: 1, ...end })))
      await act(async () => document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, ...end })))

      expect(onItemsChange).toHaveBeenCalled()
      expect(onItemsChange.mock.lastCall?.[0]).toEqual([
        { i: 'indices', t: 'indices', x: 3, y: 0, w: 2, h: 2 },
      ])
    } finally {
      await act(async () => root.unmount())
      host.remove()
    }
  })
})
