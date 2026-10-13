// @vitest-environment jsdom
// 旧 AI 草稿保留可见；任何角色均不能从网页发布 Python。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StrategyPoolDialog } from './StrategyPoolDialog'
import type { StrategyDetail } from '@/lib/api'

const mockStrategyList = vi.fn()
const mockStrategyPublish = vi.fn(async (id: string) => ({ ok: true, strategy_id: id }))
vi.mock('@/lib/api', () => ({
  api: {
    strategyList: (...args: unknown[]) => mockStrategyList(...args),
    strategyPublish: (id: string) => mockStrategyPublish(id),
  },
}))

function mkStrategy(p: Partial<StrategyDetail> & Pick<StrategyDetail, 'id' | 'name' | 'source'>): StrategyDetail {
  return {
    description: '', tags: [], research_only: false,
    execution_backend: 'polars_expr', asset_types: ['stock'], timeframes: ['1d'],
    version: '1', basic_filter: {}, params: [], params_defaults: {},
    scoring: {}, scoring_directions: {}, entry_signals: [], exit_signals: [],
    minute_exit_trigger_supported_signals: [], stop_loss: null, take_profit: null,
    trailing_stop: null, trailing_take_profit_activate: null,
    trailing_take_profit_drawdown: null, max_hold_days: null,
    order_by: '', descending: true, limit: 50,
    ...p,
  }
}

const aiDraft = mkStrategy({ id: 'ai_breakout_v1', name: 'AI突破', source: 'ai', research_only: true })
const builtinResearch = mkStrategy({ id: 'factor_rank_research', name: '因子排名研究', source: 'builtin', research_only: true })
const builtinPublic = mkStrategy({ id: 'trend_follow', name: '趋势跟随', source: 'builtin' })

let root: Root | null = null
const container = document.createElement('div')
document.body.appendChild(container)

async function render() {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  root = createRoot(container)
  await act(async () => {
    root!.render(
      <StrategyPoolDialog pool={[]} onConfirm={() => {}} onClose={() => {}} />,
    )
  })
}

function clickAiTab() {
  const tab = Array.from(container.querySelectorAll('button'))
    .find(b => b.textContent?.startsWith('AI'))
  expect(tab).toBeTruthy()
  act(() => { tab!.click() })
}

beforeEach(() => {
  mockStrategyList.mockReset()
  mockStrategyList.mockResolvedValue({ strategies: [aiDraft, builtinResearch, builtinPublic] })
  mockStrategyPublish.mockClear()
})
afterEach(() => {
  if (root) { act(() => { root!.unmount() }); root = null }
  container.innerHTML = ''
})

it('keeps old AI drafts visible without exposing Python publish or import', async () => {
  await render()
  clickAiTab()

  expect(container.textContent).toContain('AI突破')
  const publishBtn = Array.from(container.querySelectorAll('button'))
    .find(b => b.textContent?.includes('发布'))
  expect(publishBtn).toBeUndefined()
  expect(container.querySelector('input[type="file"]')).toBeNull()
  expect(container.textContent).toContain('需服务器维护者部署')
  expect(mockStrategyPublish).not.toHaveBeenCalled()
})

it('builtin research template is not offered for publish anywhere in the pool', async () => {
  await render()

  // 默认「全部」分组: research_only 项不进待选, 模板应完全不出现
  expect(container.textContent).not.toContain('因子排名研究')
  expect(container.textContent).toContain('趋势跟随')

  clickAiTab()
  // 草稿分区同样不列非 AI 来源的 research 项
  expect(container.textContent).not.toContain('因子排名研究')
})
