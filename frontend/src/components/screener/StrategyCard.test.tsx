// @vitest-environment jsdom
// 卡片保持历史计数，并独立显示后台任务状态。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { StrategyCard } from './StrategyCard'

const noop = () => {}
const base = {
  name: '分钟突破', description: '', source: 'custom' as const,
  active: false, loading: false, cardSize: 'normal' as const,
  onRun: noop, disabled: false, onSettings: noop,
  timeframeBadge: '分钟',
}

let root: Root | null = null
const container = document.createElement('div')
document.body.appendChild(container)
afterEach(() => {
  if (root) { act(() => { root!.unmount() }); root = null }
  container.innerHTML = ''
})

function render(props: Record<string, unknown>) {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  root = createRoot(container)
  act(() => { root!.render(<StrategyCard {...base} {...props} />) })
}

it('shows 待计算 hint when awaitRun and count is missing', () => {
  render({ awaitRun: true })

  expect(container.textContent).toContain('待计算')
  expect(container.textContent).not.toContain('···')
})

it('shows loading on the label while computing', () => {
  render({ awaitRun: true, computing: true })

  expect(container.querySelector('[aria-label="分钟突破计算中"]')).not.toBeNull()
  expect(container.textContent).not.toContain('待计算')
})

it('shows nothing extra for daily strategies without awaitRun', () => {
  render({})

  expect(container.textContent).not.toContain('待计算')
  expect(container.textContent).not.toContain('···')
})

it('hint yields to the real count once available', () => {
  render({ awaitRun: true, count: 7 })

  expect(container.textContent).not.toContain('待计算')
  expect(container.textContent).toContain('7')
})

it('mini size also renders the hint', () => {
  render({ awaitRun: true, cardSize: 'mini' })

  expect(container.textContent).toContain('待算')
})

it('onRun fires when the card is clicked', () => {
  const onRun = vi.fn()
  render({ awaitRun: true, onRun })

  act(() => { container.querySelector('button')!.click() })

  expect(onRun).toHaveBeenCalledTimes(1)
})

it.each(['large', 'normal', 'mini'])('preserves the previous count with loading at %s size', cardSize => {
  render({ computing: true, count: 7, cardSize })
  expect(container.textContent).toContain('7')
  expect(container.querySelector('[aria-label="分钟突破计算中"]')).not.toBeNull()
})
