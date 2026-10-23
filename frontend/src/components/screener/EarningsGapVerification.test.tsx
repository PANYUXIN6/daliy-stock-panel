// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { AuthContext } from '@/lib/auth'
import { QK } from '@/lib/queryKeys'
import type { EarningsGapVerification as VerificationData, PipelineJob, User } from '@/lib/api'
import { Screener } from '@/pages/Screener'
import { EarningsGapVerification, useEarningsGapVerification } from './EarningsGapVerification'

const mocks = vi.hoisted(() => ({
  verify: vi.fn(), pipelineJob: vi.fn(), pipelineJobCancel: vi.fn(),
  screenerStrategies: vi.fn(), screenerCachedSummary: vi.fn(), screenerCachedResult: vi.fn(),
  screenerRunAll: vi.fn(), screenerRunPreset: vi.fn(), screenerRunStatus: vi.fn(),
  pool: ['earnings_breakout_gap'], prune: vi.fn(),
  prefs: { screener_auto_run: true },
  status: { enriched: { latest_date: '2026-10-16', earliest_date: '2026-01-01' } },
}))
vi.mock('@/lib/api', () => ({ api: {
  screenerVerifyEarningsGap: mocks.verify,
  pipelineJob: mocks.pipelineJob,
  pipelineJobCancel: mocks.pipelineJobCancel,
  screenerStrategies: mocks.screenerStrategies,
  screenerCachedSummary: mocks.screenerCachedSummary,
  screenerCachedResult: mocks.screenerCachedResult,
  screenerRunAll: mocks.screenerRunAll,
  screenerRunStatus: mocks.screenerRunStatus,
  screenerRunPreset: mocks.screenerRunPreset,
  minuteRefreshStatus: async () => ({ healthy: false }),
  watchlistList: async () => ({ symbols: [] }),
  watchlistGroups: async () => ({ groups: [] }),
  monitorRulesList: async () => ({ rules: [] }),
  screenerResultColumns: async () => ({ columns: null }),
}, genRuleId: () => 'rule-id' }))
vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: mocks.prefs }),
  useDataStatus: () => ({ data: mocks.status }),
  useCapabilities: () => ({ data: { capabilities: {} } }),
  useQuoteStatus: () => ({ data: { running: false } }),
}))
vi.mock('@/lib/useStrategyPool', () => ({ useStrategyPool: () => ({
  pool: mocks.pool, prune: mocks.prune,
  addToPool: vi.fn(), removeFromPool: vi.fn(), reorderPool: (ids: string[]) => { mocks.pool = ids },
}) }))
vi.mock('@/components/StockPreviewDialog', () => ({
  StockPreviewDialog: ({ symbol }: { symbol: string | null }) => symbol ? <div aria-label="K线预览">{symbol}</div> : null,
}))
vi.mock('@/components/screener/StrategySettingsDialog', () => ({ StrategySettingsDialog: () => null }))
vi.mock('@/components/screener/DefaultStrategyParamsDialog', () => ({ DefaultStrategyParamsDialog: () => null }))
vi.mock('@/components/screener/StrategyBuilderDialog', () => ({ StrategyBuilderDialog: () => null }))
vi.mock('@/components/screener/CompositeStrategyDialog', () => ({ CompositeStrategyDialog: () => null }))
vi.mock('@/components/screener/StrategyPoolDialog', () => ({
  StrategyPoolDialog: ({ onConfirm }: { onConfirm: (ids: string[]) => void }) =>
    <button onClick={() => onConfirm([...mocks.pool, 'new-strategy'])}>添加新策略</button>,
}))
vi.mock('@/components/DatePicker', () => ({
  DatePicker: ({ onChange }: { onChange: (date: string) => void }) => <>
    <button onClick={() => onChange('2026-10-15')}>历史日期</button>
    <button onClick={() => onChange('2026-10-16')}>最新日期</button>
  </>,
}))

const admin: User = {
  id: 'a'.repeat(32), username: 'admin', role: 'admin', is_initial_admin: true,
  pro_expires_on: null, is_active: true, must_change_password: false,
  created_at: '', last_login_at: null, last_login_ip: null, login_count: 0,
}
const pending: VerificationData = {
  status: 'completed', candidate_count: 19, confirmed_count: 1, pending_count: 17,
  excluded_count: 1, checked_at: '2026-10-16T08:30:00Z',
  candidates: [
    { symbol: '600000.SH', name: '浦发银行', date: '2026-10-16', verification_status: 'pending', verification_reason: '缺少当日可用财报依据' },
    { symbol: '000001.SZ', name: '平安银行', date: '2026-10-16', verification_status: 'confirmed', verification_reason: '净利润为正且同比增长' },
    { symbol: '600519.SH', name: '贵州茅台', date: '2026-10-16', verification_status: 'excluded', verification_reason: '同比未增长' },
  ],
}

let host: HTMLDivElement
let root: Root
let qc: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  mocks.pool = ['earnings_breakout_gap']
  mocks.prefs.screener_auto_run = true
  mocks.verify.mockResolvedValue({ job_id: 'verify-1', reused: false })
  mocks.pipelineJob.mockImplementation(async (id: string) => ({
    id, status: 'running', stage: 'financials', progress: 40,
    log: [{ ts: '2026-10-16T08:30:00Z', stage: 'financials', msg: '核验浦发银行 600000.SH，待核验19只' }],
  }))
  mocks.pipelineJobCancel.mockResolvedValue({ cancelled: 'verify-1' })
  mocks.screenerStrategies.mockResolvedValue({ presets: [{
    id: 'earnings_breakout_gap', name: '业绩断层跳空', description: '',
    asset_types: ['stock'], timeframes: ['1d'], realtime_supported: false,
  }], load_errors: [] })
  mocks.screenerCachedSummary.mockResolvedValue({
    as_of: '2026-10-16', results: {
      earnings_breakout_gap: { as_of: '2026-10-16', total: 0, verification: pending },
    }, today_ever_counts: {},
  })
  mocks.screenerCachedResult.mockResolvedValue({
    result: { as_of: '2026-10-16', strategy: 'earnings_breakout_gap', total: 0, rows: [], verification: pending },
    strategy_ids_by_symbol: {}, today_ever_rows: null,
  })
  mocks.screenerRunAll.mockResolvedValue({ as_of: '2026-10-16', results: { earnings_breakout_gap: { total: 0, as_of: '2026-10-16' } } })
  window.scrollTo = vi.fn()
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

function Harness({ asOf = '2026-10-16', verification = pending, assetType = 'stock' }: {
  asOf?: string
  verification?: VerificationData
  assetType?: 'stock' | 'etf'
}) {
  const control = useEarningsGapVerification({ asOf, latestDate: '2026-10-16', assetType, verification })
  return <EarningsGapVerification verification={verification} job={control.job}
    submitting={control.busy} canVerify={control.canVerify} historical={control.historical} error={control.error}
    onVerify={control.start} />
}

async function settle() {
  for (let index = 0; index < 4; index++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function render(props: Parameters<typeof Harness>[0] = {}, user: User = admin) {
  await act(async () => root.render(
    <AuthContext.Provider value={user}><QueryClientProvider client={qc}><Harness {...props} /></QueryClientProvider></AuthContext.Provider>,
  ))
  await settle()
}
function button(text: string) {
  const found = [...host.querySelectorAll('button')].find(item => item.textContent?.trim() === text)
  if (!found) throw new Error(`找不到按钮 ${text}`)
  return found
}

it('完整核验记录含未确认股票时只显示确认数量和完成时间，不展示候选内容或计数', async () => {
  await render()
  expect(host.textContent).toContain('已确认 1')
  expect(host.textContent).toContain('核验完成，仅展示已确认的业绩断层跳空。')
  expect(host.textContent).toContain('2026/10/16')
  for (const text of ['待核验', '未通过', '完整向上缺口', '19', '浦发银行', '600000.SH', '贵州茅台', '600519.SH', '缺少当日可用财报依据', '同比未增长']) {
    expect(host.textContent).not.toContain(text)
  }
  expect(mocks.verify).not.toHaveBeenCalled()
  expect(mocks.pipelineJob).not.toHaveBeenCalled()
  expect(host.querySelector('ul')).toBeNull()
})

it('显式点击才提交核验，使用标准任务进度和取消，不因重新渲染重复提交', async () => {
  await render()
  await act(async () => button('重新核验').click())
  await settle()
  expect(mocks.verify).toHaveBeenCalledWith('2026-10-16')
  expect(mocks.pipelineJob).toHaveBeenCalledWith('verify-1')
  expect(host.textContent).toContain('40%')
  await render()
  expect(mocks.verify).toHaveBeenCalledTimes(1)
  await act(async () => button('取消任务').click())
  await settle()
  expect(mocks.pipelineJobCancel).toHaveBeenCalledWith('verify-1')
})

it.each(['running', 'pending'] as const)('重新打开已有%s核验任务只恢复进度，不显示未确认股票或另行采集', async status => {
  mocks.pipelineJob.mockResolvedValueOnce({ id: 'existing-job', status, progress: 40, log: [{ msg: '核验浦发银行 600000.SH，待核验19只' }] })
  await render({ verification: { ...pending, status: 'running', job_id: 'existing-job' } })
  expect(mocks.pipelineJob).toHaveBeenCalledWith('existing-job')
  expect(host.textContent).toContain('40%')
  expect(host.textContent).not.toContain('待核验')
  expect(host.textContent).not.toContain('浦发银行')
  expect(host.textContent).not.toContain('600000.SH')
  expect(mocks.verify).not.toHaveBeenCalled()
})

it.each(['guest', 'pro'] as const)('非管理员%s仅浏览本地结果，不发任务查询或采集命令', async role => {
  await render({ verification: { ...pending, status: 'running', job_id: 'admin-job' } }, { ...admin, role })
  expect(host.textContent).not.toContain('重新核验')
  expect(host.textContent).toContain('按需准备财务数据')
  expect(mocks.pipelineJob).not.toHaveBeenCalled()
  expect(mocks.verify).not.toHaveBeenCalled()
})

it('历史日期仅回放本地结果，不提交核验或查询采集任务，也不误报权限不足', async () => {
  await render({ asOf: '2026-10-15', verification: { ...pending, status: 'running', job_id: 'old-job' } })
  expect(host.textContent).not.toContain('重新核验')
  expect(mocks.verify).not.toHaveBeenCalled()
  expect(mocks.pipelineJob).not.toHaveBeenCalled()
  expect(host.textContent).toContain('历史结果按财报版本可用时间回放')
  expect(host.textContent).not.toContain('财务更新需管理员操作')
})

it('日期或角色切换后丢弃先前提交响应，不查询旧任务', async () => {
  let resolve!: (value: { job_id: string; reused: boolean }) => void
  mocks.verify.mockReturnValue(new Promise(done => { resolve = done }))
  await render()
  await act(async () => button('重新核验').click())
  expect(mocks.verify).toHaveBeenCalledTimes(1)
  await render({ asOf: '2026-10-15' }, { ...admin, role: 'guest' })
  await act(async () => resolve({ job_id: 'old-date-job', reused: false }))
  await settle()
  expect(mocks.pipelineJob).not.toHaveBeenCalled()
  expect(host.textContent).not.toContain('40%')
})

it('任务终态刷新已确认结果和财务查询，不再发核验POST', async () => {
  mocks.pipelineJob.mockResolvedValue({ id: 'finished-job', status: 'succeeded', progress: 100, log: [], result: {} } satisfies Partial<PipelineJob>)
  const invalidate = vi.spyOn(qc, 'invalidateQueries')
  await render({ verification: { ...pending, status: 'running', job_id: 'finished-job' } })
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['screener-cached'] })
  expect(host.textContent).toContain('核验完成，仅展示已确认的业绩断层跳空。')
  expect(mocks.verify).not.toHaveBeenCalled()
  expect(host.textContent).not.toContain('取消任务')
  expect(qc.getQueryData(QK.pipelineJob('finished-job'))).toBeTruthy()
})

it('采集失败明确显示错误，未确认股票仍不显示', async () => {
  mocks.verify.mockRejectedValueOnce(new Error('已有其他数据任务运行'))
  await render()
  await act(async () => button('重新核验').click())
  await settle()
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('已有其他数据任务运行')
  expect(host.textContent).not.toContain('600000.SH')
  expect(host.textContent).not.toContain('缺少当日可用财报依据')
})

async function renderScreener() {
  await act(async () => root.render(
    <AuthContext.Provider value={admin}><QueryClientProvider client={qc}><Screener /></QueryClientProvider></AuthContext.Provider>,
  ))
  await settle()
}

it('实际策略卡片重复点击只读取已确认结果，独立按钮才重新核验', async () => {
  mocks.screenerCachedSummary.mockResolvedValue({
    as_of: '2026-10-16', results: {
      earnings_breakout_gap: { as_of: '2026-10-16', total: 1, verification: pending },
    }, today_ever_counts: {},
  })
  mocks.screenerCachedResult.mockResolvedValue({
    result: { as_of: '2026-10-16', strategy: 'earnings_breakout_gap', total: 1,
      rows: [{ symbol: '000001.SZ', name: '平安银行', close: 10.0 }], verification: pending },
    strategy_ids_by_symbol: {}, today_ever_rows: null,
  })
  await renderScreener()
  expect(mocks.verify).not.toHaveBeenCalled()
  const card = [...host.querySelectorAll('button')].find(item => item.textContent?.includes('业绩断层跳空'))!
  await act(async () => card.click())
  await settle()
  await act(async () => card.click())
  await settle()
  expect(mocks.verify).not.toHaveBeenCalled()
  expect(mocks.screenerRunPreset).not.toHaveBeenCalled()
  expect(host.querySelector('tbody')?.textContent).toContain('000001.SZ')
  expect(host.querySelector('tbody')?.textContent).toContain('平安银行')
  for (const text of ['待核验', '未通过', '浦发银行', '600000.SH', '贵州茅台', '600519.SH']) {
    expect(host.textContent).not.toContain(text)
  }
  const confirmedRow = [...host.querySelectorAll('tbody button')].find(item => item.textContent?.includes('000001.SZ'))!
  await act(async () => (confirmedRow as HTMLButtonElement).click())
  expect(host.querySelector('[aria-label="K线预览"]')?.textContent).toBe('000001.SZ')
  await act(async () => button('重新核验').click())
  await settle()
  expect(mocks.verify).toHaveBeenCalledTimes(1)
  expect(button('核验中…').disabled).toBe(true)
})

it('业绩断层零命中明确提示条件未同时满足，不误导重新跑采集管道', async () => {
  const verification: VerificationData = { ...pending, confirmed_count: 0, pending_count: 19, excluded_count: 0 }
  mocks.screenerCachedResult.mockResolvedValue({
    result: { as_of: '2026-10-16', strategy: 'earnings_breakout_gap', total: 0, rows: [], verification },
    strategy_ids_by_symbol: {}, today_ever_rows: null,
  })
  await renderScreener()
  const card = [...host.querySelectorAll('button')].find(item => item.textContent?.includes('业绩断层跳空'))!
  await act(async () => card.click())
  await settle()
  expect(host.textContent).toContain('当日没有已确认的业绩断层跳空')
  expect(host.textContent).toContain('未发现同时满足财报时间、利润条件和完整向上跳空的股票。')
  expect(host.textContent).not.toContain('POST /api/pipeline/run')
  expect(host.textContent).not.toContain('可能数据未跑盘后管道')
  expect(host.textContent).not.toContain('待核验')
  expect(host.querySelector('tbody')).toBeNull()
  expect(mocks.verify).not.toHaveBeenCalled()
})

it.each(['completed', 'running', 'failed'] as const)('刷新页面后点击%s核验缓存只浏览结果或恢复任务', async status => {
  const verification = { ...pending, status, job_id: 'existing-job' }
  mocks.screenerCachedSummary.mockResolvedValue({
    as_of: '2026-10-16', results: {
      earnings_breakout_gap: { as_of: '2026-10-16', total: 0, verification },
    }, today_ever_counts: {},
  })
  mocks.screenerCachedResult.mockResolvedValue({
    result: { as_of: '2026-10-16', strategy: 'earnings_breakout_gap', total: 0, rows: [], verification },
    strategy_ids_by_symbol: {}, today_ever_rows: null,
  })
  for (let visit = 0; visit < 2; visit++) {
    await renderScreener()
    const card = [...host.querySelectorAll('button')].find(item => item.textContent?.includes('业绩断层跳空'))!
    await act(async () => card.click())
    await settle()
    expect(mocks.verify).not.toHaveBeenCalled()
    expect(mocks.screenerRunPreset).not.toHaveBeenCalled()
    expect(mocks.screenerRunAll).not.toHaveBeenCalled()
    if (status === 'running') {
      expect(mocks.pipelineJob).toHaveBeenCalledWith('existing-job')
      expect(button('核验中…').disabled).toBe(true)
    } else {
      expect(button('重新核验').disabled).toBe(false)
    }
    await act(async () => root.render(null))
    qc.clear()
  }
})

it('已有选股缓存但尚未核验时，独立核验按钮仍可启动首次任务', async () => {
  mocks.screenerCachedSummary.mockResolvedValue({
    as_of: '2026-10-16', results: { earnings_breakout_gap: { as_of: '2026-10-16', total: 0 } }, today_ever_counts: {},
  })
  mocks.screenerCachedResult.mockResolvedValue({
    result: { as_of: '2026-10-16', strategy: 'earnings_breakout_gap', total: 0, rows: [] },
    strategy_ids_by_symbol: {}, today_ever_rows: null,
  })
  await renderScreener()
  const card = [...host.querySelectorAll('button')].find(item => item.textContent?.includes('业绩断层跳空'))!
  await act(async () => card.click())
  await settle()
  expect(mocks.verify).not.toHaveBeenCalled()
  await act(async () => button('核验').click())
  await settle()
  expect(mocks.verify).toHaveBeenCalledTimes(1)
})

it('实际策略页缺少缓存时自动批量计算仍不发核验命令', async () => {
  mocks.screenerCachedSummary.mockResolvedValue({ as_of: '2026-10-16', results: {}, today_ever_counts: {} })
  await renderScreener()
  expect(mocks.screenerRunAll).toHaveBeenCalled()
  expect(mocks.verify).not.toHaveBeenCalled()
})

it('后台任务运行中新增策略排队，终态后继续运行且保留原策略错误', async () => {
  mocks.screenerStrategies.mockResolvedValue({ presets: [
    { id: 'earnings_breakout_gap', name: '业绩断层跳空', asset_types: ['stock'], timeframes: ['1d'] },
    { id: 'new-strategy', name: '新增策略', asset_types: ['stock'], timeframes: ['1d'] },
  ], load_errors: [] })
  mocks.screenerCachedSummary.mockResolvedValue({ as_of: '2026-10-16', results: {}, today_ever_counts: {} })
  mocks.screenerRunAll.mockResolvedValueOnce({
    run_id: 'daily-1', as_of: '2026-10-16', results: {}, pending: ['earnings_breakout_gap'],
  }).mockResolvedValue({ as_of: '2026-10-16', results: { 'new-strategy': { total: 2, as_of: '2026-10-16' } }, complete: true })
  mocks.screenerRunStatus.mockResolvedValue({ run_id: 'daily-1', pending: ['earnings_breakout_gap'], results: {}, errors: {}, error: null, done: false, started_at_ms: 1 })
  await renderScreener()
  expect(mocks.screenerRunAll).toHaveBeenCalledTimes(1)
  await act(async () => [...host.querySelectorAll('button')].find(item => item.textContent?.includes('策略池'))!.click())
  await act(async () => button('添加新策略').click())
  // 池变化触发自动补算时，missing IDs 仍包含旧批次，不应把它排队再跑。
  await renderScreener()
  await settle()
  expect(mocks.screenerRunAll).toHaveBeenCalledTimes(1)
  await act(async () => qc.setQueryData(QK.screenerRunStatus('daily-1'), {
    run_id: 'daily-1', pending: [], results: {}, errors: { earnings_breakout_gap: '利润表读取失败' }, error: null, done: true, started_at_ms: 1,
  }))
  await settle()
  expect(mocks.screenerRunAll).toHaveBeenCalledTimes(2)
  expect(mocks.screenerRunAll).toHaveBeenLastCalledWith('2026-10-16', ['new-strategy'], 'stock')
  expect(host.textContent).toContain('业绩断层跳空：利润表读取失败')
  expect(button('重试未完成策略').disabled).toBe(false)
})

function prepareQueuedBatch() {
  mocks.screenerStrategies.mockResolvedValue({ presets: [
    { id: 'earnings_breakout_gap', name: '业绩断层跳空', asset_types: ['stock'], timeframes: ['1d'] },
    { id: 'new-strategy', name: '新增策略', asset_types: ['stock'], timeframes: ['1d'] },
  ], load_errors: [] })
  mocks.screenerCachedSummary.mockResolvedValue({ as_of: '2026-10-16', results: {}, today_ever_counts: {} })
  mocks.screenerRunAll.mockResolvedValueOnce({
    run_id: 'queued-origin', as_of: '2026-10-16', results: {}, pending: ['earnings_breakout_gap'],
  }).mockImplementation(async (date: string, ids: string[]) => ({
    as_of: date, results: Object.fromEntries(ids.map(id => [id, { total: 2, as_of: date }])), complete: true,
  }))
  mocks.screenerRunStatus.mockResolvedValue({
    run_id: 'queued-origin', pending: ['earnings_breakout_gap'], results: {}, errors: {}, error: null, done: false, started_at_ms: 1,
  })
}

async function addQueuedStrategy() {
  await act(async () => [...host.querySelectorAll('button')].find(item => item.textContent?.includes('策略池'))!.click())
  await act(async () => button('添加新策略').click())
  await renderScreener()
  expect(mocks.screenerRunAll).toHaveBeenCalledTimes(1)
}

async function finishQueuedOrigin() {
  const final = {
    run_id: 'queued-origin', pending: [], results: { earnings_breakout_gap: { total: 1, as_of: '2026-10-16' } },
    errors: {}, error: null, done: true, started_at_ms: 1,
  }
  mocks.screenerRunStatus.mockResolvedValue(final)
  await act(async () => qc.setQueryData(QK.screenerRunStatus('queued-origin'), final))
  await settle()
}

it.each([false, true])('新增策略排队后跨资产往返，原批次离开期间完成=%s时仍仅提交新增一次', async finishesAway => {
  prepareQueuedBatch()
  await renderScreener()
  await addQueuedStrategy()
  await act(async () => button('ETF').click())
  await settle()
  if (finishesAway) await finishQueuedOrigin()
  expect(mocks.screenerRunAll).toHaveBeenCalledTimes(1)
  await act(async () => button('股票').click())
  await settle()
  if (!finishesAway) await finishQueuedOrigin()
  expect(mocks.screenerRunAll).toHaveBeenCalledTimes(2)
  expect(mocks.screenerRunAll).toHaveBeenLastCalledWith('2026-10-16', ['new-strategy'], 'stock')
  await act(async () => button('ETF').click())
  await settle()
  await act(async () => button('股票').click())
  await settle()
  expect(mocks.screenerRunAll).toHaveBeenCalledTimes(2)
})

it('待提交策略按日期保留，历史视图不提交其他日期的队列', async () => {
  prepareQueuedBatch()
  await renderScreener()
  await addQueuedStrategy()
  // 关闭自动计算只影响后续历史页，手动新增形成的队列仍须继续。
  mocks.prefs.screener_auto_run = false
  await act(async () => button('历史日期').click())
  await settle()
  await finishQueuedOrigin()
  expect(mocks.screenerRunAll).toHaveBeenCalledTimes(1)
  await act(async () => button('最新日期').click())
  await settle()
  expect(mocks.screenerRunAll).toHaveBeenCalledTimes(2)
  expect(mocks.screenerRunAll).toHaveBeenLastCalledWith('2026-10-16', ['new-strategy'], 'stock')
})

it('首返前新增的待提交策略切出后仍保留，返回原资产后接续计算', async () => {
  prepareQueuedBatch()
  let resolve!: (value: unknown) => void
  mocks.screenerRunAll.mockReset().mockReturnValueOnce(new Promise(done => { resolve = done }))
    .mockResolvedValue({ as_of: '2026-10-16', results: { 'new-strategy': { total: 2, as_of: '2026-10-16' } }, complete: true })
  await renderScreener()
  await addQueuedStrategy()
  // 已排队的手动请求独立于自动补算，防止漏队列被自动计算偶然补救。
  mocks.prefs.screener_auto_run = false
  await act(async () => button('ETF').click())
  await settle()
  await act(async () => resolve({
    run_id: 'queued-origin', as_of: '2026-10-16', results: {}, pending: ['earnings_breakout_gap'],
  }))
  await settle()
  await finishQueuedOrigin()
  expect(mocks.screenerRunAll).toHaveBeenCalledTimes(1)
  await act(async () => button('股票').click())
  await settle()
  expect(mocks.screenerRunAll).toHaveBeenCalledTimes(2)
  expect(mocks.screenerRunAll).toHaveBeenLastCalledWith('2026-10-16', ['new-strategy'], 'stock')
})

it.each(['succeeded', 'failed'] as const)('股票任务切到 ETF 再返回恢复跟踪并展示%s终态，不重复提交', async outcome => {
  const runId = `resume-${outcome}`
  mocks.screenerCachedSummary.mockResolvedValue({ as_of: '2026-10-16', results: {}, today_ever_counts: {} })
  mocks.screenerRunAll.mockResolvedValue({
    run_id: runId, as_of: '2026-10-16', results: {}, pending: ['earnings_breakout_gap'],
  })
  mocks.screenerRunStatus.mockResolvedValue({
    run_id: runId, pending: ['earnings_breakout_gap'], results: {}, errors: {}, error: null, done: false, started_at_ms: 1,
  })
  await renderScreener()
  expect(mocks.screenerRunAll).toHaveBeenCalledTimes(1)
  expect(mocks.screenerRunStatus).toHaveBeenCalledWith(runId)
  await act(async () => button('ETF').click())
  await settle()
  const pollsBeforeReturn = mocks.screenerRunStatus.mock.calls.length
  const errors = outcome === 'failed' ? { earnings_breakout_gap: '切换期间利润表读取失败' } : {}
  const results = outcome === 'succeeded' ? { earnings_breakout_gap: { total: 7, as_of: '2026-10-16' } } : {}
  mocks.screenerRunStatus.mockResolvedValue({ run_id: runId, pending: [], results, errors, error: null, done: true, started_at_ms: 1 })
  if (outcome === 'succeeded') {
    mocks.screenerCachedSummary.mockResolvedValue({ as_of: '2026-10-16', results, today_ever_counts: {} })
  }
  expect(host.textContent).not.toContain('切换期间利润表读取失败')
  await act(async () => button('股票').click())
  await settle()
  expect(mocks.screenerRunStatus.mock.calls.length).toBeGreaterThan(pollsBeforeReturn)
  expect(mocks.screenerRunStatus).toHaveBeenLastCalledWith(runId)
  expect(mocks.screenerRunAll).toHaveBeenCalledTimes(1)
  const card = [...host.querySelectorAll('button')].find(item => item.textContent?.includes('业绩断层跳空'))!
  expect(card.textContent).not.toContain('···')
  if (outcome === 'failed') {
    expect(host.textContent).toContain('业绩断层跳空：切换期间利润表读取失败')
    expect(button('重试未完成策略').disabled).toBe(false)
  } else {
    expect(card.textContent).toContain('7')
  }
})

it('首返尚未到达时切到 ETF，股票响应保留到切回后继续跟踪', async () => {
  let resolve!: (value: unknown) => void
  mocks.screenerCachedSummary.mockResolvedValue({ as_of: '2026-10-16', results: {}, today_ever_counts: {} })
  mocks.screenerRunAll.mockReturnValue(new Promise(done => { resolve = done }))
  mocks.screenerRunStatus.mockResolvedValue({
    run_id: 'late-first-response', pending: [], results: {},
    errors: { earnings_breakout_gap: '首返后后台计算失败' }, error: null, done: true, started_at_ms: 1,
  })
  await renderScreener()
  expect(mocks.screenerRunAll).toHaveBeenCalledTimes(1)
  await act(async () => button('ETF').click())
  await settle()
  await act(async () => resolve({
    run_id: 'late-first-response', as_of: '2026-10-16', results: {}, pending: ['earnings_breakout_gap'],
  }))
  await settle()
  expect(mocks.screenerRunStatus).not.toHaveBeenCalled()
  expect(host.textContent).not.toContain('首返后后台计算失败')
  await act(async () => button('股票').click())
  await settle()
  expect(mocks.screenerRunStatus).toHaveBeenCalledWith('late-first-response')
  expect(host.textContent).toContain('业绩断层跳空：首返后后台计算失败')
  expect(mocks.screenerRunAll).toHaveBeenCalledTimes(1)
})

it('实际策略卡片选择历史日期时沿用原本地选股入口', async () => {
  mocks.screenerRunAll.mockImplementation(async (date: string) => ({ as_of: date, results: {} }))
  mocks.screenerRunPreset.mockResolvedValue({ as_of: '2026-10-15', strategy: 'earnings_breakout_gap', total: 0, rows: [] })
  await renderScreener()
  await act(async () => button('历史日期').click())
  await settle()
  const card = [...host.querySelectorAll('button')].find(item => item.textContent?.includes('业绩断层跳空'))!
  await act(async () => card.click())
  await settle()
  expect(mocks.verify).not.toHaveBeenCalled()
  expect(mocks.screenerRunPreset).toHaveBeenCalledWith('earnings_breakout_gap', undefined, '2026-10-15', undefined, 'stock', '1d')
  expect(host.textContent).toContain('历史结果按财报版本可用时间回放')
})
