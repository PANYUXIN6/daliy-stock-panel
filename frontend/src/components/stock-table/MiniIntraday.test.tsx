// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it } from 'vitest'
import type { MinuteKlineRow } from '@/lib/api'
import { MiniIntraday } from './MiniIntraday'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let cleanup = async () => {}
afterEach(async () => { await cleanup() })

function minute(close: number, volume: number, index: number): MinuteKlineRow {
  return {
    datetime: `2026-10-22 09:${String(31 + index).padStart(2, '0')}:00`,
    open: 10, high: Math.max(10, close), low: Math.min(10, close), close, volume, amount: null,
  }
}

async function render(rows: MinuteKlineRow[], width = 100) {
  const host = document.createElement('div')
  const root = createRoot(host)
  const update = async (next: MinuteKlineRow[]) => {
    await act(async () => root.render(<MiniIntraday rows={next} prevClose={10} width={width} />))
  }
  cleanup = async () => { await act(async () => root.unmount()) }
  await update(rows)
  return { host, update }
}

it('shows volume below the price plot, with proportional bars and interval direction', async () => {
  const { host } = await render([minute(10.1, 100, 0), minute(10, 200, 1), minute(10, 50, 2)])
  const bars = Array.from(host.querySelectorAll('g[aria-label="成交量"] rect'))
  expect(bars).toHaveLength(3)
  const heights = bars.map(bar => Number(bar.getAttribute('height')))
  expect(heights[1] / heights[0]).toBe(2)
  expect(heights[0] / heights[2]).toBe(2)
  expect(bars.map(bar => bar.getAttribute('fill'))).toEqual(['#C74040', '#2D9B65', '#7A7A85'])
  const lowestPrice = Math.max(...Array.from(host.querySelectorAll('polyline')).flatMap(line => (
    (line.getAttribute('points') ?? '').split(' ').map(point => Number(point.split(',')[1]))
  )))
  expect(Math.min(...bars.map(bar => Number(bar.getAttribute('y'))))).toBeGreaterThan(lowestPrice)
  expect(host.querySelector('svg')?.getAttribute('aria-label')).toBe('分时走势与成交量')
})

it('aggregates dense minute volumes without losing volume or overflowing the narrow chart', async () => {
  const rows = Array.from({ length: 240 }, (_, index) => minute(10 + index / 1000, index + 1, index))
  const { host } = await render(rows, 70)
  const bars = Array.from(host.querySelectorAll('g[aria-label="成交量"] rect'))
  expect(bars.length).toBeLessThanOrEqual(28)
  const total = bars.reduce((sum, bar) => sum + Number(bar.textContent?.replace('成交量 ', '').replaceAll(',', '')), 0)
  expect(total).toBe(rows.reduce((sum, row) => sum + row.volume, 0))
  for (const bar of bars) {
    const x = Number(bar.getAttribute('x'))
    expect(x).toBeGreaterThanOrEqual(0)
    expect(x + Number(bar.getAttribute('width'))).toBeLessThanOrEqual(70)
  }
})

it('does not invent bars for zero, negative, missing or nonfinite volume', async () => {
  const rows = [0, -1, Number.NaN, Number.POSITIVE_INFINITY, null, 100]
    .map((volume, index) => minute(10 + index / 100, volume as number, index))
  const { host } = await render(rows)
  const bars = host.querySelectorAll('g[aria-label="成交量"] rect')
  expect(bars).toHaveLength(1)
  expect(bars[0].textContent).toBe('成交量 100')
  expect(host.innerHTML).not.toContain('NaN')
  expect(host.innerHTML).not.toContain('Infinity')
})

it('keeps the full price area when no positive volume is available', async () => {
  const { host } = await render([minute(10.1, 0, 0), minute(9.9, 0, 1)])
  expect(host.querySelector('g[aria-label="成交量"]')).toBeNull()
  const priceLine = Array.from(host.querySelectorAll('polyline')).at(-1)
  expect(priceLine?.getAttribute('points')).toBe('0.0,3.0 100.0,53.0')
})

it('preserves chart size and hook order across empty, populated and empty states', async () => {
  const { host, update } = await render([])
  expect(host.querySelector('svg')?.getAttribute('aria-label')).toBe('暂无分时')
  await update([minute(10.1, 100, 0), minute(10.2, 200, 1)])
  expect(host.querySelector('g[aria-label="成交量"]')).not.toBeNull()
  await update([])
  expect(host.querySelector('svg')?.getAttribute('width')).toBe('100')
  expect(host.querySelector('svg')?.getAttribute('height')).toBe('56')
  expect(host.querySelector('svg')?.getAttribute('aria-label')).toBe('暂无分时')
})
