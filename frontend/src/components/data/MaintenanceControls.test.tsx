// @vitest-environment jsdom
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { FinancialSyncPanel } from './FinancialSyncPanel'
import { AdjFactorSyncConfig } from './AdjFactorSyncConfig'
import { MinuteSyncConfig } from './MinuteSyncConfig'
import { RegimeMaintenancePanel } from './RegimeMaintenancePanel'
import { RegimeConfigCard } from './RegimeConfigCard'
import { QuoteConfigCard } from './QuoteConfigCard'
import { DepthConfigContent } from './DepthConfigCard'
import { ExtDataStatCard } from '@/components/ext-data/ExtDataStatCard'
import { Data } from '@/pages/Data'
import { SealedBadge } from '@/components/SealedBadge'
import { QK } from '@/lib/queryKeys'
import { formatScheduleTimePart } from '@/lib/format'
import type { ExtDataConfig } from '@/lib/api'

const mocks = vi.hoisted(() => ({
  financialStatus: vi.fn(), financialExecutionStatus: vi.fn(), financialSync: vi.fn(), pipelineJob: vi.fn(), pipelineJobCancel: vi.fn(),
  syncMinuteSingle: vi.fn(), regimeMainline: vi.fn(), regimeRecompute: vi.fn(),
  regimeMainlineRecompute: vi.fn(), mainlineFilterUpdate: vi.fn(), extDataPresetFetch: vi.fn(),
  syncIndexDaily: vi.fn(), quoteStatus: vi.fn(), preferences: vi.fn(), capabilities: vi.fn(),
  dataStatus: vi.fn(), pipelineRun: vi.fn(), updateInstrumentsSchedule: vi.fn(),
  updateDepthPollingInterval: vi.fn(),
  runLimitLadderFix: vi.fn(), capabilityMatrix: vi.fn(),
  updatePipelineRegimeEnabled: vi.fn(),
  collectionSyncCheck: vi.fn(), pipelineAdjFactorRun: vi.fn(), intradayRefresh: vi.fn(),
  updateOrdinaryMinute: vi.fn(), syncMinute: vi.fn(), extDataPresetsSync: vi.fn(), dataClear: vi.fn(),
}))
vi.mock('@/lib/api', () => ({ api: {
  ...mocks,
  settings: async () => ({ mode: 'api_key' }),
  regimeCoverage: async () => ({ rows: 0 }),
  pipelineJobs: async () => ({ jobs: [] }),
  extDataList: async () => ({ items: [] }),
  dataSources: async () => ({ builtin: [], plugins: [], custom: [] }),
  quoteInterval: async () => ({ interval: 30, min_interval: 6, max_interval: 60 }),
} }))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
vi.mock('@/components/ext-data/ExtDataPullPanel', () => ({ ExtDataPullPanel: () => null }))

let host: HTMLDivElement
let root: Root
let qc: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  window.scrollTo = vi.fn()
  mocks.financialStatus.mockResolvedValue({ available: false, tables: {} })
  mocks.financialExecutionStatus.mockResolvedValue({ last_sync: {}, syncing: false, active_job_id: null })
  mocks.collectionSyncCheck.mockResolvedValue({ requires_confirmation: false, message: null, last_synced_at: null })
  mocks.pipelineAdjFactorRun.mockResolvedValue({ job_id: 'adj-new' })
  mocks.intradayRefresh.mockResolvedValue({ ok: true })
  mocks.updateOrdinaryMinute.mockResolvedValue({ ordinary_minute_enabled: false })
  mocks.financialSync.mockResolvedValue({ job_id: 'financial-new', synced: { started: true } })
  mocks.pipelineJob.mockImplementation(async (id: string) => ({ id, status: 'running', progress: 40, log: [{ msg: '同步利润表' }] }))
  mocks.pipelineJobCancel.mockResolvedValue({ status: 'cancelling' })
  mocks.syncMinuteSingle.mockResolvedValue({ job_id: 'minute-new', status: 'started' })
  mocks.regimeMainline.mockResolvedValue({ rows: [], leaders: [], filter: { min_members: 8, max_members: 200, blacklist: ['宽基'], exclude_st: true } })
  mocks.regimeRecompute.mockResolvedValue({ ok: true, computed: 10 })
  mocks.regimeMainlineRecompute.mockResolvedValue({ ok: true, rows: 20 })
  mocks.mainlineFilterUpdate.mockResolvedValue({})
  mocks.extDataPresetFetch.mockResolvedValue({ status: 'ok', rows: 12 })
  mocks.quoteStatus.mockResolvedValue({ enabled: true, running: true, paused: false, is_trading_hours: true })
  mocks.syncIndexDaily.mockResolvedValue({ job_id: 'index-new' })
  mocks.preferences.mockResolvedValue({ minute_sync_days: 5, minute_sync_segment_days: 20 })
  mocks.capabilities.mockResolvedValue({ capabilities: { 'kline.daily.batch': true } })
  mocks.dataStatus.mockResolvedValue({ instruments: { rows: 1 }, storage: {} })
  mocks.pipelineRun.mockResolvedValue({ job_id: 'pipeline-new' })
  mocks.syncMinute.mockResolvedValue({ job_id: 'minutes-batch-new', status: 'started' })
  mocks.extDataPresetsSync.mockResolvedValue({ job_id: 'classifications-new', reused: false })
  mocks.updateInstrumentsSchedule.mockResolvedValue({ hour: 9, minute: 11 })
  mocks.updateDepthPollingInterval.mockResolvedValue({ depth_polling_interval: 30 })
  mocks.runLimitLadderFix.mockResolvedValue({ ok: true, count: 10, msg: '已更新 10 只标的的盘口' })
  mocks.capabilityMatrix.mockResolvedValue({ capabilities: [{ id: 'daily', usable: true }, { id: 'realtime', usable: true }] })
  mocks.updatePipelineRegimeEnabled.mockResolvedValue({ pipeline_regime_enabled: true })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  qc.clear()
  host.remove()
})
async function settle() {
  for (let i = 0; i < 4; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function render(node: ReactNode) {
  await act(async () => root.render(<MemoryRouter><QueryClientProvider client={qc}>{node}</QueryClientProvider></MemoryRouter>))
  await settle()
}
function button(text: string) {
  const found = [...host.querySelectorAll('button')].find(item => item.textContent?.trim() === text)
  if (!found) throw new Error(`找不到按钮 ${text}`)
  return found
}
async function input(element: HTMLInputElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(element, value)
    element.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

it('财务维护展示共享画像并引导按股获取，不再提供全量任务', async () => {
  mocks.financialStatus.mockResolvedValue({ tables: { income: { rows: 12, symbols: 1 } } })
  await render(<FinancialSyncPanel />)
  expect(host.textContent).toContain('12 条')
  expect(host.querySelector('a')?.getAttribute('href')).toBe('/financials')
  expect(host.querySelectorAll('button')).toHaveLength(0)
  expect(mocks.financialSync).not.toHaveBeenCalled()
})

it('单标的分钟采集明确本地日历交易日口径并保持参数，失败会显示原因', async () => {
  const onJobStart = vi.fn()
  await render(<MinuteSyncConfig hasCap onJobStart={onJobStart} />)
  expect(host.textContent).toContain('随全量同步已关闭')
  expect(host.textContent).toContain('批量分钟历史首次回溯 5 自然日；已有数据从最新时间增量更新到现在')
  expect(host.textContent).toContain('按本地交易日历采集指定标的最近 1 ~ 30 个交易日')
  expect(host.textContent).toContain('交易日数')
  await input(host.querySelector<HTMLInputElement>('input[placeholder="600000.SH"]')!, ' 600519.SH ')
  await input(host.querySelector<HTMLInputElement>('input[type="number"]')!, '30')
  await act(async () => button('获取单标的').click())
  await settle()
  expect(mocks.syncMinuteSingle).toHaveBeenCalledWith('600519.SH', 30)
  expect(mocks.collectionSyncCheck).toHaveBeenCalledWith({ kind: 'minute', symbol: '600519.SH', days: 30 })
  expect(onJobStart).toHaveBeenCalledWith('minute-new')
  mocks.syncMinuteSingle.mockRejectedValueOnce(new Error('已有同步任务正在执行'))
  await act(async () => button('获取单标的').click())
  await settle()
  expect(host.querySelector('[role="alert"]')?.textContent).toBe('已有同步任务正在执行')
  await input(host.querySelector<HTMLInputElement>('input[type="number"]')!, '31')
  expect(button('获取单标的').disabled).toBe(true)
})

it.each([
  ['向前扩展约 20 个交易日', 20],
  ['向前扩展约 365 个交易日', 365],
] as const)('分钟扩展入口 %s 明确估算范围并保留原请求参数', async (label, days) => {
  const onJobStart = vi.fn()
  await render(<MinuteSyncConfig hasCap onJobStart={onJobStart} />)
  expect(host.textContent).not.toContain('获取最近 1 年')
  expect(host.textContent).toContain('按交易日数估算自然日区间')
  expect(host.textContent).toContain('无本地数据时从当前回溯')
  expect(host.textContent).toContain('节假日会使实际交易日数有偏差')
  const target = [...host.querySelectorAll('button')].find(item => item.textContent?.includes(label))
  expect(target).toBeDefined()
  await act(async () => target!.click())
  await settle()
  expect(mocks.syncMinute).toHaveBeenCalledExactlyOnceWith(days, true)
  expect(onJobStart).toHaveBeenCalledWith('minutes-batch-new')
})

it('环境维护提交所选日期区间，全量和仅主线操作保持各自范围', async () => {
  await render(<RegimeMaintenancePanel />)
  const dates = host.querySelectorAll<HTMLInputElement>('input[type="date"]')
  await input(dates[0], '2026-09-01')
  await input(dates[1], '2026-09-30')
  await act(async () => button('重算区间').click())
  await settle()
  expect(mocks.regimeRecompute).toHaveBeenLastCalledWith('2026-09-01', '2026-09-30')
  await act(async () => button('全量重算环境').click())
  await settle()
  expect(mocks.regimeRecompute).toHaveBeenLastCalledWith(undefined, undefined)
  await act(async () => button('仅重算主线').click())
  await settle()
  expect(mocks.regimeMainlineRecompute).toHaveBeenCalledTimes(1)
})

it('主线过滤使用已保存口径，剔除 ST 改动仍触发环境与主线全量重算', async () => {
  await render(<RegimeMaintenancePanel />)
  await act(async () => button('保存并重算').click())
  await settle()
  expect(mocks.mainlineFilterUpdate).toHaveBeenCalledWith({ min_members: 8, max_members: 200, blacklist: ['宽基'], exclude_st: true })
  expect(mocks.regimeMainlineRecompute).toHaveBeenCalledTimes(1)
  expect(mocks.regimeRecompute).not.toHaveBeenCalled()
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="统计剔除 ST 股"]')!.click())
  await act(async () => button('保存并重算').click())
  await settle()
  expect(mocks.mainlineFilterUpdate).toHaveBeenLastCalledWith({ min_members: 8, max_members: 200, blacklist: ['宽基'], exclude_st: false })
  expect(mocks.regimeRecompute).toHaveBeenCalledWith()
})

it('环境重算执行期间阻止重复提交，失败返回具体原因', async () => {
  let reject: (error: Error) => void = () => {}
  mocks.regimeRecompute.mockImplementationOnce(() => new Promise((_resolve, rejectPromise) => { reject = rejectPromise }))
  await render(<RegimeMaintenancePanel />)
  await act(async () => button('全量重算环境').click())
  await settle()
  expect(button('全量重算环境').disabled).toBe(true)
  expect(button('仅重算主线').disabled).toBe(true)
  expect(button('保存并重算').disabled).toBe(true)
  await act(async () => reject(new Error('本地指标数据尚未采集')))
  await settle()
  expect(host.querySelector('[role="alert"]')?.textContent).toBe('本地指标数据尚未采集')
  expect(button('全量重算环境').disabled).toBe(false)
  await act(async () => button('仅重算主线').click())
  await settle()
  expect(host.querySelector('[role="alert"]')).toBeNull()
})

it.each([['ext_gn_ths', '概念'], ['ext_hy_ths', '行业']])('%s 分类入口继续使用内置转换接口', async (id, label) => {
  const config: ExtDataConfig = { id, label, mode: 'snapshot', fields: [], description: `在${label}页手动获取`, created_at: '', updated_at: '' }
  await render(<ExtDataStatCard config={config} onDelete={vi.fn()} deleting={false} />)
  expect(host.textContent).toContain('在数据页获取并由后台保存')
  expect(host.textContent).not.toContain(`在${label}页手动获取`)
  await act(async () => host.querySelector<HTMLButtonElement>(`[aria-label="配置${label}"]`)!.click())
  await act(async () => host.querySelector<HTMLElement>('summary')!.click())
  await act(async () => button(`获取${label}数据`).click())
  await settle()
  expect(mocks.extDataPresetFetch).toHaveBeenCalledWith(id)
  expect(mocks.collectionSyncCheck).toHaveBeenCalledWith({ kind: 'ext', config_id: id })
  expect(host.textContent).toContain('采集完成 · 12 行')
})

it('同步期间暂停手动行情采集，自动采集开关和间隔设置仍可操作', async () => {
  const onRefresh = vi.fn()
  const onToggle = vi.fn()
  const onShowIntervalEdit = vi.fn()
  await render(<QuoteConfigCard
    enabled running paused isTrading lastFetchMs={null}
    intervalS={30} intervalMin={6} intervalMax={60} loading={false}
    onToggle={onToggle} toggling={false} showIntervalEdit={false}
    onShowIntervalEdit={onShowIntervalEdit} onIntervalChange={vi.fn()}
    onRefresh={onRefresh} refreshing={false} canRefresh
  />)
  expect(button('手动获取行情').disabled).toBe(true)
  expect(host.textContent).toContain('数据同步期间暂停行情采集')
  await act(async () => button('手动获取行情').click())
  expect(onRefresh).not.toHaveBeenCalled()
  await act(async () => host.querySelector<HTMLButtonElement>('[role="switch"]')!.click())
  expect(onToggle).toHaveBeenCalledWith(false)
  await act(async () => host.querySelector<HTMLButtonElement>('[title="设置轮询间隔"]')!.click())
  expect(onShowIntervalEdit).toHaveBeenCalledOnce()
})

it('数据页使用后台行情暂停状态禁用手动获取', async () => {
  mocks.quoteStatus.mockResolvedValue({ enabled: true, running: true, paused: true, is_trading_hours: true })
  await render(<Data />)
  expect(button('手动获取行情').disabled).toBe(true)
  expect(host.textContent).toContain('数据同步期间暂停行情采集')
})

it.each([['financial:all', false], ['pipeline', true]])('后台任务 %s 的行情暂停状态与实际采集边界一致', async (requestKey, paused) => {
  qc.setQueryData(QK.pipelineJobs, { active_id: 'active', jobs: [] })
  mocks.pipelineJob.mockResolvedValue({ id: 'active', status: 'running', request_key: requestKey,
    progress: 10, log: [{ msg: '后台采集中' }] })
  await render(<Data />)
  expect(button('手动获取行情').disabled).toBe(paused)
  expect(host.textContent?.includes('数据同步期间暂停行情采集')).toBe(paused)
})

it('数据页展示盘前与交易日盘后自动同步时间，手动维护仍可用', async () => {
  mocks.capabilities.mockResolvedValue({ capabilities: { 'kline.daily.batch': true, 'adj_factor': true, 'kline.minute.batch': true } })
  mocks.preferences.mockResolvedValue({
    instruments_schedule: { hour: 9, minute: 10 },
    pipeline_schedule: { hour: 16, minute: 0 },
  })
  mocks.dataStatus.mockResolvedValue({
    instruments: { rows: 1 }, storage: {},
    last_instruments_run: '2026-10-09T09:10:00+08:00',
    next_instruments_run: '2026-10-12T09:10:00+08:00',
    last_pipeline_run: '2026-10-09T15:40:00+08:00',
    next_pipeline_run: '2026-10-12T16:00:00+08:00',
  })
  await render(<Data />)
  const heading = [...host.querySelectorAll('h3')].find(node => node.textContent === '同步安排')!
  const card = heading.parentElement!.parentElement!
  expect(card.textContent).toContain('09:10')
  expect(card.textContent).toContain(formatScheduleTimePart('2026-10-09T15:40:00+08:00'))
  expect(card.textContent).toContain('交易日 · 盘后自动同步')
  expect(card.textContent).toContain('16:00 北京时间')
  expect(card.textContent).toContain('当天行情与指标核验通过后')
  for (const [title, label] of [['个股维表', '自动'], ['日 K', '随同步'], ['除权因子', '随同步'], ['Enriched', '随同步'], ['指数', '随同步'], ['分钟 K', '不随同步'], ['市场环境', '不随同步']]) {
    const cardTitle = [...host.querySelectorAll('h3')].find(node => node.textContent === title)!
    expect(cardTitle.parentElement!.textContent).toContain(label)
  }
  await act(async () => card.querySelector<HTMLButtonElement>('[aria-label="编辑盘前更新时间"]')!.click())
  expect(card.textContent).toContain('不晚于 09:15')
  await input(card.querySelectorAll<HTMLInputElement>('input[type="number"]')[1], '11')
  await act(async () => button('保存').click())
  await settle()
  expect(mocks.updateInstrumentsSchedule).toHaveBeenCalledWith(9, 11)
  await act(async () => button('同步行情与基础数据').click())
  await settle()
  expect(mocks.pipelineRun).toHaveBeenCalledOnce()
  expect(mocks.collectionSyncCheck).toHaveBeenCalledWith({ kind: 'pipeline' })
  expect(mocks.pipelineJob).toHaveBeenCalledWith('pipeline-new')
})

it.each([
  ['同步分钟历史数据', { kind: 'minute_batch', days: 5 }, 'syncMinute', [5, false], 'minutes-batch-new'],
  ['同步板块分类', { kind: 'classifications' }, 'extDataPresetsSync', [], 'classifications-new'],
] as const)('大模块入口 %s 只提交一个后台任务并跟踪进度', async (label, request, method, args, jobId) => {
  mocks.capabilityMatrix.mockResolvedValue({ capabilities: [{ id: 'daily', usable: true }, { id: 'minute', usable: true }, { id: 'financial', usable: true }] })
  await render(<Data />)
  expect([...host.querySelectorAll('button')].some(item => item.textContent === '立即同步')).toBe(false)
  await act(async () => button(label).click())
  await settle()
  expect(mocks.collectionSyncCheck).toHaveBeenCalledExactlyOnceWith(request)
  expect(mocks[method]).toHaveBeenCalledExactlyOnceWith(...args)
  expect(mocks.pipelineJob).toHaveBeenCalledWith(jobId)
  expect(mocks.extDataPresetFetch).not.toHaveBeenCalled()
  expect(mocks.syncMinuteSingle).not.toHaveBeenCalled()
  for (const title of ['同步行情与基础数据', '同步分钟历史数据', '同步板块分类']) {
    expect(button(title).disabled).toBe(true)
  }
})

it.each([false, true])('数据页分钟批量提示首次自然日回溯与增量更新，随行情同步为 %s', async (minuteAuto) => {
  mocks.preferences.mockResolvedValue({ minute_sync_days: 7, minute_sync_enabled: minuteAuto })
  mocks.capabilityMatrix.mockResolvedValue({ capabilities: [{ id: 'daily', usable: true }, { id: 'minute', usable: true }] })
  await render(<Data />)
  const card = (title: string) => [...host.querySelectorAll('h3')]
    .find(node => node.textContent === title)!.parentElement!.parentElement!
  const range = '首次回溯 7 自然日；已有数据从最新时间增量更新到现在'
  expect(card('分钟历史数据').textContent).toContain(range)
  expect(card('分钟历史数据').textContent).toContain('可独立更新，不受随行情同步开关影响')
  if (minuteAuto) {
    expect(card('行情与基础数据').textContent).toContain(`同时更新分钟 K：${range}`)
  } else {
    expect(card('行情与基础数据').textContent).not.toContain('同时更新分钟 K')
  }
  expect(mocks.syncMinute).not.toHaveBeenCalled()
  expect(mocks.pipelineRun).not.toHaveBeenCalled()
})

it('模块重复确认期间全页同步互锁，取消不采集，确认仅提交一次', async () => {
  mocks.capabilityMatrix.mockResolvedValue({ capabilities: [{ id: 'minute', usable: true }, { id: 'financial', usable: true }, { id: 'realtime', usable: true }, { id: 'depth5', usable: true }] })
  mocks.collectionSyncCheck.mockResolvedValue(duplicate)
  await render(<Data />)
  await act(async () => button('同步板块分类').click())
  await settle()
  expect(button('同步分钟历史数据').disabled).toBe(true)
  expect([...host.querySelectorAll('button')].some(item => item.textContent === '同步财务数据')).toBe(false)
  expect(button('手动获取行情').disabled).toBe(true)
  expect(button('更新五档盘口').disabled).toBe(true)
  await act(async () => dialogButton('取消').click())
  expect(mocks.extDataPresetsSync).not.toHaveBeenCalled()
  expect(button('同步板块分类').disabled).toBe(false)
  await act(async () => button('同步板块分类').click())
  await settle()
  await act(async () => dialogButton('重新拉取').click())
  await settle()
  expect(mocks.extDataPresetsSync).toHaveBeenCalledOnce()
})

it('大模块显示后台冲突错误，失败后允许重新提交', async () => {
  mocks.extDataPresetsSync.mockRejectedValueOnce(new Error('已有同步任务正在执行'))
  await render(<Data />)
  await act(async () => button('同步板块分类').click())
  await settle()
  expect(host.querySelector('[role="alert"]')?.textContent).toBe('已有同步任务正在执行')
  expect(button('同步板块分类').disabled).toBe(false)
})

it.each([
  ['同步板块分类', 'classifications', 'failed', ['ext-data-rows', 'ext_gn_ths']],
] as const)('模块 %s 终态刷新对应本地缓存，保留已分块落盘的失败结果', async (label, requestKey, status, affectedKey) => {
  mocks.capabilityMatrix.mockResolvedValue({ capabilities: [{ id: 'financial', usable: true }] })
  mocks.pipelineJob.mockImplementation(async (id: string) => ({ id, request_key: requestKey, status, stage: 'done', progress: 100, log: [], result: {}, error: status === 'failed' ? '行业请求失败' : null }))
  await render(<Data />)
  qc.setQueryData(affectedKey, { rows: [1] })
  qc.setQueryData(['kline-batch', 'unaffected'], { rows: [2] })
  await act(async () => button(label).click())
  await settle()
  expect(qc.getQueryState(affectedKey)?.isInvalidated).toBe(true)
})

it('环境计算明确随手动全量同步执行，并保留原范围偏好开关', async () => {
  await render(<RegimeConfigCard />)
  expect(host.textContent).toContain('随手动同步计算')
  expect(host.textContent).toContain('此开关控制手动“同步行情与基础数据”时是否一并计算')
  await act(async () => host.querySelector<HTMLButtonElement>('[role="checkbox"]')!.click())
  await settle()
  expect(mocks.updatePipelineRegimeEnabled).toHaveBeenCalledWith(true)
})

it('盘口配置保留盘中轮询间隔，说明盘后自动更新与手动补采', async () => {
  mocks.capabilities.mockResolvedValue({ capabilities: { 'depth5.batch': true } })
  mocks.preferences.mockResolvedValue({ depth_polling_interval: 10, depth_finalize_time: { hour: 15, minute: 2 } })
  await render(<DepthConfigContent />)
  expect(host.textContent).toContain('盘后自动更新')
  expect(host.textContent).toContain('点击“更新五档盘口”手动补采')
  const intervals = host.querySelectorAll<HTMLInputElement>('input[type="number"]')
  expect(intervals).toHaveLength(1)
  await input(intervals[0], '30')
  await act(async () => intervals[0].dispatchEvent(new FocusEvent('focusout', { bubbles: true })))
  await settle()
  expect(mocks.updateDepthPollingInterval).toHaveBeenCalledWith(30)
})

it('数据页手动更新盘口后刷新连板梯队和看板数据', async () => {
  mocks.capabilityMatrix.mockResolvedValue({ capabilities: [{ id: 'daily', usable: true }, { id: 'depth5', usable: true }] })
  mocks.capabilities.mockResolvedValue({ capabilities: { 'depth5.batch': true } })
  await render(<Data />)
  const ladderKey = QK.limitLadder('2026-10-09')
  const overviewKey = QK.overviewMarket('2026-10-09')
  qc.setQueryData(ladderKey, { rows: [] })
  qc.setQueryData(overviewKey, { limit: {} })
  await act(async () => button('更新五档盘口').click())
  await settle()
  expect(mocks.runLimitLadderFix).toHaveBeenCalledOnce()
  expect(mocks.collectionSyncCheck).toHaveBeenCalledWith({ kind: 'depth' })
  expect(qc.getQueryState(ladderKey)?.isInvalidated).toBe(true)
  expect(qc.getQueryState(overviewKey)?.isInvalidated).toBe(true)
})

it('盘口更新请求期间阻止重复采集，并显示后台失败原因和请求错误', async () => {
  mocks.capabilityMatrix.mockResolvedValue({ capabilities: [{ id: 'daily', usable: true }, { id: 'depth5', usable: true }] })
  let resolve: (value: unknown) => void = () => {}
  mocks.runLimitLadderFix.mockImplementationOnce(() => new Promise(resolvePromise => { resolve = resolvePromise }))
  await render(<Data />)
  await act(async () => button('更新五档盘口').click())
  await settle()
  expect(button('更新中…').disabled).toBe(true)
  await act(async () => button('更新中…').click())
  expect(mocks.runLimitLadderFix).toHaveBeenCalledOnce()
  await act(async () => resolve({ ok: false, count: 0, msg: '五档盘口采集失败' }))
  await settle()
  expect(host.querySelector('[role="alert"]')?.textContent).toBe('五档盘口采集失败')
  mocks.runLimitLadderFix.mockRejectedValueOnce(new Error('数据同步期间暂停盘口采集'))
  await act(async () => button('更新五档盘口').click())
  await settle()
  expect(host.querySelector('[role="alert"]')?.textContent).toBe('数据同步期间暂停盘口采集')
})

it.each([false, true])('盘口能力缺失或后台同步暂停时禁止手动更新（暂停=%s）', async paused => {
  mocks.capabilityMatrix.mockResolvedValue({ capabilities: [{ id: 'depth5', usable: paused }] })
  mocks.quoteStatus.mockResolvedValue({ paused })
  await render(<Data />)
  expect(button('更新五档盘口').disabled).toBe(true)
  await act(async () => button('更新五档盘口').click())
  expect(mocks.runLimitLadderFix).not.toHaveBeenCalled()
})

it('业务页盘口提示保留修正结果，并引导数据页维护', async () => {
  function LocationProbe() {
    return <span role="status">{useLocation().pathname}</span>
  }
  await render(<>
    <SealedBadge degraded={false} hasDepth isHistorical={false} sealedReady
      sealedCountsUp={{ real: 5, fake: 2, pending: 0 }} rawUp={7} />
    <LocationProbe />
  </>)
  await act(async () => button('修正').click())
  const popup = document.body.querySelector<HTMLDivElement>('.fixed.z-50')!
  expect(popup.textContent).toContain('真封 5')
  expect(popup.textContent).toContain('收盘后可在数据页手动更新最终盘口')
  const maintenance = [...popup.querySelectorAll('button')].find(node => node.textContent === '前往数据页')!
  await act(async () => maintenance.click())
  expect(host.querySelector('[role="status"]')?.textContent).toBe('/data')
  expect(mocks.runLimitLadderFix).not.toHaveBeenCalled()
})

it('指数获取在收到 HTTP 响应前失败时仍在弹窗展示错误', async () => {
  mocks.syncIndexDaily.mockRejectedValueOnce(new TypeError('Failed to fetch'))
  await render(<Data />)
  const heading = [...host.querySelectorAll('h3')].find(node => node.textContent === '指数')!
  await act(async () => heading.parentElement!.querySelector<HTMLButtonElement>('[title="设置"]')!.click())
  await settle()
  await act(async () => button('获取数据').click())
  await settle()
  expect(host.querySelector('[role="alert"]')?.textContent).toBe('Failed to fetch')
  expect(button('获取数据').disabled).toBe(false)
})

function dialogButton(text: string) {
  return [...document.querySelector('[role="dialog"]')!.querySelectorAll('button')].find(node => node.textContent === text)!
}
const duplicate = { requires_confirmation: true, message: '相同范围已经同步，是否重新拉取？', last_synced_at: null }

it.each(['adj_factor_provider', 'minute_data_provider'] as const)(
  '维护入口切换 %s 后旧确认失效，重新操作会再次检查', async providerField => {
    mocks.preferences.mockResolvedValue({ [providerField]: 'provider_a' })
    mocks.collectionSyncCheck.mockResolvedValue(duplicate)
    const adj = providerField === 'adj_factor_provider'
    await render(adj
        ? <AdjFactorSyncConfig hasCap providerDisplay="测试源" />
        : <MinuteSyncConfig hasCap />)
    if (!adj) await input(host.querySelector<HTMLInputElement>('input[placeholder="600000.SH"]')!, '600000.SH')
    const label = adj ? '全历史同步除权因子' : '获取单标的'
    const collect = adj ? mocks.pipelineAdjFactorRun : mocks.syncMinuteSingle
    await act(async () => button(label).click())
    await settle()
    expect(document.querySelector('[role="dialog"]')).not.toBeNull()
    const oldConfirm = dialogButton('重新拉取')
    await act(async () => { qc.setQueryData(QK.preferences, { [providerField]: 'provider_b' }) })
    await settle()
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    await act(async () => oldConfirm.click())
    expect(collect).not.toHaveBeenCalled()
    await act(async () => button(label).click())
    await settle()
    expect(mocks.collectionSyncCheck).toHaveBeenCalledTimes(2)
    await act(async () => dialogButton('重新拉取').click())
    await settle()
    expect(collect).toHaveBeenCalledOnce()
  },
)

it('除权独立入口重复确认后沿用任务机制，检查错误不会发起采集', async () => {
  mocks.collectionSyncCheck.mockResolvedValueOnce(duplicate)
  const onJobStart = vi.fn()
  await render(<AdjFactorSyncConfig hasCap providerDisplay="测试源" onJobStart={onJobStart} />)
  await act(async () => button('全历史同步除权因子').click())
  await settle()
  expect(mocks.pipelineAdjFactorRun).not.toHaveBeenCalled()
  await act(async () => dialogButton('重新拉取').click())
  await settle()
  expect(mocks.pipelineAdjFactorRun).toHaveBeenCalledOnce()
  expect(onJobStart).toHaveBeenCalledWith('adj-new')
  mocks.collectionSyncCheck.mockRejectedValueOnce(new Error('本地记录暂不可读取'))
  await act(async () => button('全历史同步除权因子').click())
  await settle()
  expect(host.querySelector('[role="alert"]')?.textContent).toBe('本地记录暂不可读取')
  expect(mocks.pipelineAdjFactorRun).toHaveBeenCalledOnce()
})

it('单标的分钟切换输入会取消旧确认，后续只提交新的代码和天数', async () => {
  mocks.collectionSyncCheck.mockResolvedValue(duplicate)
  await render(<MinuteSyncConfig hasCap />)
  const symbol = host.querySelector<HTMLInputElement>('input[placeholder="600000.SH"]')!
  const days = host.querySelector<HTMLInputElement>('input[type="number"]')!
  await input(symbol, '600000.SH')
  await act(async () => button('获取单标的').click())
  await settle()
  await input(symbol, '600519.SH')
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  expect(mocks.syncMinuteSingle).not.toHaveBeenCalled()
  await input(days, '10')
  await act(async () => button('获取单标的').click())
  await settle()
  await act(async () => dialogButton('重新拉取').click())
  await settle()
  expect(mocks.syncMinuteSingle).toHaveBeenCalledExactlyOnceWith('600519.SH', 10)
})

it('行情手动入口通过本地预检查，取消重复更新不调用采集', async () => {
  mocks.collectionSyncCheck.mockResolvedValueOnce(duplicate)
  await render(<Data />)
  await act(async () => button('手动获取行情').click())
  await settle()
  expect(mocks.collectionSyncCheck).toHaveBeenCalledWith({ kind: 'quotes' })
  await act(async () => dialogButton('取消').click())
  expect(mocks.intradayRefresh).not.toHaveBeenCalled()
  await act(async () => button('手动获取行情').click())
  await settle()
  expect(mocks.intradayRefresh).toHaveBeenCalledOnce()
})

it('指数重复确认保留原请求窗口，取消不启动同步任务', async () => {
  mocks.collectionSyncCheck.mockResolvedValue(duplicate)
  await render(<Data />)
  const heading = [...host.querySelectorAll('h3')].find(node => node.textContent === '指数')!
  await act(async () => heading.parentElement!.querySelector<HTMLButtonElement>('[title="设置"]')!.click())
  await settle()
  await act(async () => button('获取数据').click())
  await settle()
  const request = mocks.collectionSyncCheck.mock.calls[0][0]
  expect(request.kind).toBe('index_daily')
  expect(request.days).toBeGreaterThan(30)
  await act(async () => dialogButton('取消').click())
  expect(mocks.syncIndexDaily).not.toHaveBeenCalled()
  await act(async () => button('获取数据').click())
  await settle()
  await act(async () => dialogButton('重新拉取').click())
  await settle()
  expect(mocks.syncIndexDaily).toHaveBeenCalledExactlyOnceWith(request.days)
})

it('普通分钟自动采集开关独立保存，关闭不会启动行情或全量同步', async () => {
  mocks.capabilityMatrix.mockResolvedValue({ capabilities: [{ id: 'minute', usable: true }, { id: 'realtime', usable: true }] })
  mocks.preferences.mockResolvedValue({ ordinary_minute_enabled: true })
  await render(<Data />)
  const toggle = host.querySelector<HTMLButtonElement>('[role="switch"][aria-label="普通分钟采集"]')!
  expect(toggle.getAttribute('aria-checked')).toBe('true')
  expect(host.textContent).toContain('独立于实时行情开关')
  mocks.preferences.mockResolvedValue({ ordinary_minute_enabled: false })
  await act(async () => toggle.click())
  await settle()
  expect(mocks.updateOrdinaryMinute).toHaveBeenCalledExactlyOnceWith(false)
  expect(toggle.getAttribute('aria-checked')).toBe('false')
  expect(mocks.pipelineRun).not.toHaveBeenCalled()
  expect(mocks.intradayRefresh).not.toHaveBeenCalled()
})

it('普通分钟能力暂不可用时仍可关闭已启用的采集，关闭后禁止重新开启', async () => {
  mocks.capabilityMatrix.mockResolvedValue({ capabilities: [{ id: 'minute', usable: false }] })
  mocks.preferences.mockResolvedValue({ ordinary_minute_enabled: true })
  await render(<Data />)
  const toggle = host.querySelector<HTMLButtonElement>('[role="switch"][aria-label="普通分钟采集"]')!
  expect(toggle.disabled).toBe(false)
  mocks.preferences.mockResolvedValue({ ordinary_minute_enabled: false })
  await act(async () => toggle.click())
  await settle()
  expect(mocks.updateOrdinaryMinute).toHaveBeenCalledExactlyOnceWith(false)
  expect(toggle.disabled).toBe(true)
  expect(mocks.collectionSyncCheck).not.toHaveBeenCalled()
})


it.each([
  ['指数', 'daily', 'daily_data_provider', 'syncIndexDaily'],
  ['行情', 'realtime', 'realtime_data_provider', 'intradayRefresh'],
  ['盘口', 'depth5', 'depth5_data_provider', 'runLimitLadderFix'],
] as const)('%s 实际来源变化撤销重复确认，必须按新来源重新预检查', async (_label, cap, preference, method) => {
  mocks.preferences.mockResolvedValue({ [preference]: 'provider_a' })
  mocks.capabilityMatrix.mockResolvedValue({ capabilities: [{ id: cap, usable: true, effective: 'provider_a' }] })
  mocks.collectionSyncCheck.mockResolvedValue(duplicate)
  await render(<Data />)
  if (cap === 'daily') {
    const heading = [...host.querySelectorAll('h3')].find(node => node.textContent === '指数')!
    await act(async () => heading.parentElement!.querySelector<HTMLButtonElement>('[title="设置"]')!.click())
    await settle()
  }
  const trigger = cap === 'daily' ? '获取数据' : cap === 'realtime' ? '手动获取行情' : '更新五档盘口'
  await act(async () => button(trigger).click())
  await settle()
  const oldConfirm = dialogButton('重新拉取')
  // 配置事件先更新实际能力矩阵；usable 保持 true，不能继续使用旧源证据。
  await act(async () => qc.setQueryData(QK.capabilityMatrix, { capabilities: [{ id: cap, usable: true, effective: 'provider_b' }] }))
  await settle()
  expect([...document.querySelectorAll('[role="dialog"]')].some(node => node.textContent?.includes('重新拉取'))).toBe(false)
  await act(async () => oldConfirm.click())
  expect(mocks[method]).not.toHaveBeenCalled()
  await act(async () => qc.setQueryData(QK.preferences, { [preference]: 'provider_b' }))
  await settle()
  await act(async () => button(trigger).click())
  await settle()
  expect(mocks.collectionSyncCheck).toHaveBeenCalledTimes(2)
  await act(async () => dialogButton('重新拉取').click())
  await settle()
  expect(mocks[method]).toHaveBeenCalledOnce()
})

it('清库确认默认聚焦取消，约束键盘焦点，Escape 关闭并恢复触发按钮', async () => {
  await render(<Data />)
  const trigger = button('清除数据')
  trigger.focus()
  await act(async () => trigger.click())
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 25)) })
  const dialog = document.querySelector<HTMLElement>('[role="dialog"]')!
  expect(dialog).not.toBeNull()
  expect(dialog.getAttribute('aria-modal')).toBe('true')
  const cancel = dialogButton('取消')
  const confirm = dialogButton('清除数据')
  expect(document.activeElement).toBe(cancel)
  // jsdom 不计算布局；两按钮在真实布局中均可见。
  for (const node of [cancel, confirm]) Object.defineProperty(node, 'offsetParent', { configurable: true, value: dialog })
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true })))
  expect(document.activeElement).toBe(confirm)
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })))
  expect(document.activeElement).toBe(cancel)
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  expect(document.activeElement).toBe(trigger)
})


it('财务维护只查询本地画像，采集服务离线也保留浏览入口', async () => {
  mocks.financialStatus.mockResolvedValue({ available: true, tables: { income: { rows: 24, symbols: 3 } } })
  mocks.financialExecutionStatus.mockRejectedValue(new Error('采集服务未连接'))
  await render(<FinancialSyncPanel />)
  expect(host.textContent).toContain('24 条')
  expect(mocks.financialExecutionStatus).not.toHaveBeenCalled()
  expect(mocks.financialSync).not.toHaveBeenCalled()
})

it('清库请求进行中禁止 Escape 或取消关闭确认框', async () => {
  let resolve: (result: { deleted_files: number }) => void = () => {}
  mocks.dataClear.mockImplementationOnce(() => new Promise(done => { resolve = done }))
  await render(<Data />)
  await act(async () => button('清除数据').click())
  await settle()
  await act(async () => dialogButton('清除数据').click())
  await settle()
  expect(dialogButton('取消').disabled).toBe(true)
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(document.querySelector('[role="dialog"]')).not.toBeNull()
  await act(async () => dialogButton('取消').click())
  expect(document.querySelector('[role="dialog"]')).not.toBeNull()
  await act(async () => resolve({ deleted_files: 0 }))
  await settle()
  expect(document.querySelector('[role="dialog"]')).toBeNull()
})
