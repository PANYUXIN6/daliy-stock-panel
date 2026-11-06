// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { EtfReference } from '@/lib/api'
import EtfReferenceChart from './EtfReferenceChart'

const mocks = vi.hoisted(() => ({ setOption: vi.fn(), dispose: vi.fn() }))
vi.mock('echarts', () => ({ init: (container: HTMLElement) => ({ setOption: mocks.setOption, dispose: () => { mocks.dispose(); container.innerHTML = '' }, resize: vi.fn() }) }))
vi.mock('@/lib/theme', () => ({ useChartTheme: () => ({}) }))
let host: HTMLDivElement
let root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  mocks.setOption.mockClear()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
})
const data: EtfReference = {
  state: 'ready', as_of: '2026-09-30', checked_at: '2026-10-28', update_failed: false,
  rows: [{ date: '2026-09-30', ratio: 0.02, etf_assets_yuan: 800, float_cap_yuan: 40000 }],
}
it('只画最终占比线，并以百分比展示单位和截止日', async () => {
  await act(async () => root.render(<EtfReferenceChart data={data} loading={false} error={false} onRetry={() => {}} />))
  const option = mocks.setOption.mock.calls.at(-1)![0]
  expect(option.series).toHaveLength(1)
  expect(option.series[0].data).toEqual([0.02])
  expect(option.yAxis.axisLabel.formatter(0.02)).toBe('2.00%')
  expect(option.tooltip.valueFormatter(0.02)).toBe('2.000%')
  expect(host.textContent).toContain('国家队持仓占比参考')
  expect(host.textContent).toContain('截至 2026-09-30')
})
it('首次空数据、加载、失败和重试可区分', async () => {
  const retry = vi.fn()
  const render = async (loading: boolean, error: boolean) => act(async () => root.render(
    <EtfReferenceChart loading={loading} error={error} onRetry={retry} />))
  await render(true, false)
  expect(host.textContent).toContain('加载中')
  await render(false, false)
  expect(host.textContent).toContain('暂无已对齐的数据')
  await render(false, true)
  await act(async () => host.querySelector('button')!.click())
  expect(retry).toHaveBeenCalledOnce()
  expect(mocks.setOption).not.toHaveBeenCalled()
})
it('更新失败仍展示已保存曲线', async () => {
  await act(async () => root.render(<EtfReferenceChart data={{ ...data, update_failed: true }} loading={false} error={false} onRetry={() => {}} />))
  expect(host.querySelector('[role="img"]')).not.toBeNull()
  expect(host.textContent).toContain('更新失败，显示已保存数据')
})

it('首次采集失败显示具体环节，兼容没有失败详情的旧状态', async () => {
  const render = async (failure_stage?: string) => act(async () => root.render(
    <EtfReferenceChart data={{ ...data, rows: [], as_of: null, update_failed: true, failure_stage }} loading={false} error={false} onRetry={() => {}} />))
  await render('深交所ETF历史份额')
  expect(host.textContent).toContain('数据采集失败，尚无可展示数据')
  expect(host.textContent).toContain('未完成：深交所ETF历史份额')
  expect(host.textContent).not.toContain('暂无已对齐的数据')
  await render()
  expect(host.textContent).toContain('请在数据页查看采集任务的失败记录')
})

it('切日期时图表销毁不移除加载状态，加载完成和空数据均可继续切换', async () => {
  const render = async (next: EtfReference | undefined, loading = false) => act(async () => root.render(
    <EtfReferenceChart data={next} loading={loading} error={false} onRetry={() => {}} />))
  await render(data)
  await render(undefined, true)
  expect(host.querySelector('[role="status"]')?.textContent).toContain('加载中')
  await render({ ...data, rows: [], as_of: null })
  expect(host.textContent).toContain('暂无已对齐的数据')
  await render(data)
  expect(host.querySelector('[role="img"]')).not.toBeNull()
})
