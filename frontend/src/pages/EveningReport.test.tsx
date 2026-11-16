// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { EveningReport } from './EveningReport'
import { EveningReportContent } from '@/components/evening-report/EveningReportContent'
import { ApiError, type EveningReport as Report, type EveningFeedback, type EveningObservation, type EveningPerformance, type EveningPersonalRecord, type User } from '@/lib/api'
import { AuthContext } from '@/lib/auth'
import { QK } from '@/lib/queryKeys'
const mocks = vi.hoisted(() => ({ get: vi.fn(), dates: vi.fn(), status: vi.fn(), add: vi.fn(), rebuild: vi.fn(), personal: vi.fn(), feedback: vi.fn(), watched: [] as {symbol: string}[] }))
vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), api: {
  eveningReportDates: mocks.dates,
  eveningReport: mocks.get,
  eveningReportStatus: mocks.status,
  watchlistList: async () => ({ symbols: mocks.watched }),
  watchlistGroups: async () => ({ groups: [{ id: 'group-a', name: '观察组', color: 'blue' }] }),
  watchlistAdd: mocks.add, eveningReportRebuild: mocks.rebuild,
  eveningPersonal: mocks.personal, eveningFeedback: mocks.feedback,
} }))
vi.mock('@/lib/useCollectorJob', () => ({ useCollectorJob: (id: string | null) => ({ running: false, cancel: {}, data: id ? { status: 'succeeded', progress: 100, error: null } : undefined, message: id ?? '' }) }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: ({ symbol, onClose }: { symbol: string | null; onClose: () => void }) => symbol ? <div role="dialog">当前行情 {symbol}<button onClick={onClose}>关闭</button></div> : null }))
const judge = { value: null, failed_reasons: [], unknown_reasons: ['ATR预热不足，缺失近端窗口中的必要价格，当前风险不能计算'] }
function fixture(version = 'v1'): Report {
  return {
    date: '2026-11-02', trade_date: '2026-11-02', next_trade_date: '2026-11-03', cutoff: '2026-11-02 17:00', generated_at: '2026-11-02 18:00', version, parent_version: null, kind: version === 'v1' ? 'original' : 'revision', mode: 'live', status: 'published', rule_version: 'test-v1', disclaimer: '', summary: `${version}固定摘要`, market: { phase: 'unclassified', phase_reason: '阶段未命中规则' }, selected: ['000001.SZ'],
    records: [{ symbol: '000001.SZ', name: '样例股份', purpose: '已确认核心跟踪', regime: '10%', selected: false, limitations: ['市场政策证据不足', '位置尚未确认'], wait_conditions: ['等待下一交易日正式收盘并确认窗口恢复'], risk: { risk_now: null, paused: false, clear_count: 0, judgement: judge }, position: judge, paths: [{ id: 'p', source: 'concept', sector_id: 'c', sector_name: '示例概念', round_id: 'r', round_status: 'active', selected_path: true, selection_role: 'trend', features: { raw_close: 10, close: 99, R5: 0.12, R20: 0.2, amount: 200000000 }, calculation_reasons: ['地位分所需同组数据缺失'], identities: [{ type: 'trend', status: 'confirmed', valid: true, intervals: [{ confirmed_at: '2026-10-30', ended_at: null }] }, { type: 'board', status: 'unverified', valid: null, intervals: [{ confirmed_at: '2026-10-29', ended_at: null }] }], resilience: { status: 'untested', quality: null, R: null, D: null, C: null, F: null, pressure_days: 0, mature_events: 0, ongoing: false, local_weak: null, unknown_reasons: [] }, catchup: { ...judge, references: [] }, judgements: {}, selection: judge, limitations: ['该概念的连板维持待核验'] }] }],
    sectors: [], events: [{ date: '2026-11-02', symbol: '000001.SZ', type: 'confirmed', reason: '趋势首次确认' }], statistics: { scope_count: 100, eligible_count: 90, scored_count: 80, unknown_count: 10, failed_count: 10, report_count: 1, selected_count: 0, purpose_counts: { 已确认核心跟踪: 1, 风险暂停: 0 }, amount_coverage: [], sector_coverage: {}, zero_selection_reasons: ['市场政策、风险与位置必要证据存在缺口'] }, provenance: { source_date: null },
  }
}
let host: HTMLDivElement, root: Root, client: QueryClient
let saved: Map<string, EveningFeedback>
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.get.mockReset().mockImplementation(async (_date: string, version: string) => fixture(version || 'v1'))
  mocks.dates.mockReset().mockResolvedValue({ dates: [{ date: '2026-11-02', status: 'published', versions: [{ version: 'v2', kind: 'revision', generated_at: '2026-11-02 19:00' }] }] })
  mocks.status.mockReset().mockImplementation(async (date: string) => ({ date: date || '2026-11-02', status: 'waiting', reason: '等待本轮完整分类观察' }))
  mocks.add.mockReset(); mocks.rebuild.mockReset(); mocks.watched = []
  saved = new Map()
  mocks.personal.mockReset().mockImplementation(async (date: string, version: string) => {
    const report = await mocks.get(date, version)
    return { user_id: 'test', date, version, ready: true, records: (report.records ?? []).filter((row: Report['records'][number]) => report.selected.includes(row.symbol)).map((row: Report['records'][number]) => personalRecord(row, date, version)) }
  })
  mocks.feedback.mockReset().mockImplementation(async request => {
    const feedback = { starred: request.starred, outcome: request.outcome, revision: request.expected_revision + 1, updated_at: '2026-11-02T18:30:00+08:00' }
    saved.set(request.symbol, feedback)
    return { episode_id: request.symbol, ...feedback }
  })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
function personalRecord(row: Report['records'][number], date: string, version: string): EveningPersonalRecord {
  const performance = (window: number): EveningPerformance => ({ window, target_date: null, as_of: date, return: null, status: 'not_due', reason: '目标日未到', trading_constraints: [], data_version: null })
  return { id: `${version}:${row.symbol}`, version, day: date, month: date.slice(0, 7), symbol: row.symbol, name: row.name, episode_id: row.symbol, observation: row.observation ?? null,
    feedback: saved.get(row.symbol) ?? { starred: false, outcome: null, revision: 0, updated_at: null }, started_at: date, last_seen: date, ended_at: null, first_reference: `${version}:${row.symbol}`, historical: false,
    performance: { '1': performance(1), '3': performance(3), '5': performance(5) } }
}
async function render(role = 'guest') { await act(async () => root.render(<AuthContext.Provider value={{ id: 'test', role } as User}><QueryClientProvider client={client}><MemoryRouter><EveningReport /></MemoryRouter></QueryClientProvider></AuthContext.Provider>)); await settle(); await settle(); await settle() }
async function settle() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) }) }
async function click(text: string) { const button = [...document.querySelectorAll('button')].find(b => b.textContent === text); expect(button).toBeTruthy(); await act(async () => button!.click()); await settle() }
async function change(label: string, value: string) { const element = host.querySelector(`[aria-label="${label}"]`) as HTMLInputElement; await act(async () => { const setter = Object.getOwnPropertyDescriptor(element instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype, 'value')!.set!; setter.call(element, value); element.dispatchEvent(new Event(element instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true })) }); await settle() }

it('默认展示最近交易日而非旧报告日期，状态仅出现一次且重试使用该日', async () => {
  const status = { date: '2026-11-13', status: 'waiting', reason: '未保留该日完整输入，无法按当时口径恢复生成' }
  mocks.status.mockResolvedValue(status)
  mocks.get.mockResolvedValue(status)
  mocks.rebuild.mockRejectedValue(new Error(status.reason))
  await render('admin')
  expect((host.querySelector('[aria-label="报告日期"]') as HTMLInputElement).value).toBe('2026-11-13')
  expect(host.textContent).toContain('最近交易日 · 2026-11-13')
  expect(host.textContent?.match(/2026-11-13 · 等待数据/g)).toHaveLength(1)
  expect(host.textContent).not.toContain('今日生成状态')
  expect(mocks.get.mock.calls.every(([date]) => date === '2026-11-13')).toBe(true)
  await click('重试生成')
  expect(mocks.rebuild).toHaveBeenCalledWith('2026-11-13', 'live')
})
it('手动选择历史日期后只显示所选日期状态，最近日失败不串入', async () => {
  mocks.status.mockImplementation(async (date: string) => date
    ? { date, status: 'waiting', reason: '所选日期等待输入' }
    : { date: '2026-11-13', status: 'failed', reason: '最近日失败原因' })
  mocks.get.mockImplementation(mocks.status)
  await render()
  expect(host.textContent).toContain('最近日失败原因')
  await change('报告日期', '2026-11-02')
  expect(host.textContent).toContain('所选日期等待输入')
  expect(host.textContent?.match(/2026-11-02 · 等待数据/g)).toHaveLength(1)
  expect(host.textContent).not.toContain('最近日失败原因')
  expect(host.textContent).not.toContain('2026-11-13')
})
it('没有已知交易日时显示日历提示，不向自然日请求报告或提供生成操作', async () => {
  mocks.dates.mockResolvedValue({ dates: [] })
  mocks.status.mockResolvedValue({ date: '', status: 'waiting', reason: '交易日历尚未就绪，暂时无法确定最近交易日' })
  await render('admin')
  expect(host.textContent).toContain('暂时无法确定最近交易日')
  expect(host.textContent).not.toContain('生成管理')
  expect(mocks.get).not.toHaveBeenCalled()
})

it('只按最终名单顺序展示，最多5只且每股只出现一次', async () => {
  const report = fixture()
  report.records = Array.from({ length: 7 }, (_, i) => ({ ...report.records[0], symbol: `stock${i}`, name: `股票${i}` }))
  report.selected = ['stock6', 'stock4', 'stock2', 'stock0', 'stock5', 'stock1', 'stock3']
  mocks.get.mockResolvedValue(report)
  await render()
  expect([...host.querySelectorAll('[data-report-stock] button')].map(node => node.textContent)).toEqual(['股票6', '股票4', '股票2', '股票0', '股票5'])
  expect(host.textContent).not.toContain('完整明细')
  expect(host.textContent).not.toContain('固定摘要')
})
it('零入选明确空态，不能回退显示未入选对象或审计计数', async () => {
  const report = fixture(); report.selected = []; report.statistics.report_count = 148; report.summary = '完整跟踪148只'
  report.statistics.zero_selection_reasons.push('观察位置条件满足', '全股主要用途允许精选')
  mocks.get.mockResolvedValue(report)
  await render()
  expect(host.textContent).toContain('暂无符合条件的观察股票')
  expect(host.textContent).toContain('风险与位置必要证据存在缺口')
  expect(host.textContent).toContain('观察所需的价格位置条件未满足')
  expect(host.textContent).toContain('尚无满足全部观察条件的股票')
  expect(host.textContent).not.toContain('观察位置条件满足')
  expect(host.textContent).not.toContain('全股主要用途允许精选')
  expect(host.querySelectorAll('article')).toHaveLength(0)
  expect(host.textContent).not.toContain('148')
  expect(host.textContent).not.toContain('样例股份')
  expect(host.textContent).toContain('2026-11-02 · 等待数据')
})
it('使用实际入选题材及原始收盘价，比例和成交额单位正确且限制保留', async () => {
  const report = fixture()
  const selected = report.records[0].paths[0]
  report.records[0].paths = [{ ...selected, id: 'old', selected_path: false, sector_name: '旧题材', limitations: ['非入选路径的排名细节'] }, { ...selected, sector_name: '实际题材', selection_role: 'candidate' }]
  mocks.get.mockResolvedValue(report)
  await render()
  for (const text of ['实际题材', '近期出现走强变化', '10.00', '+12.00%', '+20.00%', '2.00亿', '市场政策证据不足', '位置尚未确认']) expect(host.textContent).toContain(text)
  for (const text of ['旧题材', '非入选路径的排名细节', '99.00', 'H_t', '主要用途']) expect(host.textContent).not.toContain(text)
})
it('缺入选依据不猜角色，缺原始价格不替换复权价或填零', async () => {
  const report = fixture(); report.records[0].paths[0].selected_path = false
  mocks.get.mockResolvedValue(report)
  await render()
  expect(host.textContent).toContain('本期筛选入选，详细理由未记录')
  expect(host.querySelector('td[title="旧版未记录"]')?.textContent).toBe('—')
  expect(host.textContent).not.toContain('示例概念')
  expect(host.textContent).not.toContain('收盘 0.00')
})
it('版本和日期切换关闭当前行情，不保留上一报告股票', async () => {
  mocks.get.mockImplementation(async (date: string, version: string) => {
    const report = fixture(version || 'v1'); report.date = date; report.trade_date = date
    report.records[0].name = version === 'v2' ? '修订股票' : '原版股票'
    return report
  })
  await render(); await act(async () => (host.querySelector('[data-report-stock] button') as HTMLButtonElement).click())
  expect(host.querySelector('[role="dialog"]')).not.toBeNull()
  await change('报告版本', 'v2')
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  expect(host.textContent).toContain('修订股票')
  expect(host.textContent).not.toContain('原版股票')
  await act(async () => (host.querySelector('[data-report-stock] button') as HTMLButtonElement).click()); await change('报告日期', '2026-10-30')
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  expect(host.textContent).toContain('原版股票')
})
it('添加失败保留分组重试，成功只刷新自选且已有自选不可重复提交', async () => {
  mocks.add.mockRejectedValueOnce(new Error('连接失败')).mockImplementationOnce(async () => { mocks.watched = [{symbol:'000001.SZ'}]; return {symbols:mocks.watched} })
  await render(); await click('查看详情'); const invalidate = vi.spyOn(client, 'invalidateQueries')
  await click('加入自选'); await click('观察组')
  expect(host.textContent).toContain('连接失败')
  await click('按原分组重试')
  expect(mocks.add).toHaveBeenNthCalledWith(2, '000001.SZ', '每日晚报 2026-11-02', 'group-a')
  expect(invalidate.mock.calls.map(args => args[0]?.queryKey)).toEqual([QK.watchlist, ['watchlist-enriched']])
  const button = [...host.querySelectorAll('button')].find(b => b.textContent === '已在自选')!
  expect(button.disabled).toBe(true)
  await act(async () => button.click()); expect(mocks.add).toHaveBeenCalledTimes(2)
})
it('添加中禁用按钮，阻止重复请求', async () => {
  let resolve!: (value: unknown) => void
  mocks.add.mockImplementation(() => new Promise(done => {resolve = done}))
  await render(); await click('查看详情'); await click('加入自选'); await click('观察组')
  const button = [...host.querySelectorAll('button')].find(b => b.textContent === '添加中…')!
  expect(button.disabled).toBe(true)
  await act(async () => button.click()); expect(mocks.add).toHaveBeenCalledTimes(1)
  await act(async () => {mocks.watched = [{symbol:'000001.SZ'}]; resolve({symbols:mocks.watched})})
})
it('未生成与读取失败不冒充有效空结果，普通用户没有生成操作', async () => {
  const waiting = {date:'2026-11-02',status:'waiting',reason:'等待正式行情'}
  mocks.get.mockResolvedValueOnce(waiting)
  mocks.status.mockResolvedValueOnce(waiting)
  await render()
  expect(host.textContent).toContain('等待正式行情')
  expect(host.textContent).not.toContain('暂无符合条件的观察股票')
  expect(host.textContent).not.toContain('生成管理')
  mocks.get.mockRejectedValueOnce(new Error('该日读取失败'))
  await change('报告日期', '2026-10-30')
  expect(host.textContent).toContain('该日读取失败')
  expect(mocks.rebuild).not.toHaveBeenCalled()
})
it('管理员显式提交，失败可见且切日后迟到结果不串任务', async () => {
  mocks.rebuild.mockRejectedValueOnce(new Error('输入未就绪'))
  mocks.get.mockImplementation(async(date:string)=>({...fixture(),date,trade_date:date}))
  await render('admin'); expect(mocks.rebuild).not.toHaveBeenCalled()
  await click('生成修订版'); expect(host.textContent).toContain('输入未就绪')
  let resolve!: (value: unknown)=>void
  mocks.rebuild.mockImplementation(()=>new Promise(done=>{resolve=done}))
  await click('生成修订版'); await change('报告日期','2026-10-30')
  await act(async()=>resolve({job_id:'old-job',status:'generating'})); await settle()
  expect(host.textContent).not.toContain('old-job')
})
it('恢复计算保留真实生成说明，不展示历史审计长页', async () => {
  mocks.get.mockResolvedValue({...fixture(),late_generation:true,recovery_note:'恢复计算、当时未发布',generated_at:'2026-11-03 09:30'})
  await render()
  expect(host.textContent).toContain('恢复计算、当时未发布 · 实际生成时间：2026-11-03 09:30')
  expect(host.textContent).not.toContain('覆盖与方法')
})

it('观察名单保留未归类、压力未结束和过热提示，不从完整记录补位', async () => {
  const report = fixture()
  report.selection_policy_version = 'observation-v2'
  report.market = { state: 'neutral', phase: 'unclassified', phase_reason: 'no_rule_match' }
  const row = report.records[0]
  const passed = { value: true, failed_reasons: [], unknown_reasons: [] }
  row.selected = true
  row.limitations = ['位置过热：仅作后续跟踪，留意价格回落']
  row.position = { ...passed, value: false, failed_reasons: ['不过热'], observation_eligibility: passed }
  row.risk = { risk_now: false, paused: false, clear_count: 0, judgement: passed }
  row.paths[0].selection = passed
  row.paths[0].calculation_reasons = []
  row.paths[0].limitations = ['压力进行中，成熟历史不能掩盖当前冲击']
  row.paths[0].resilience.ongoing = true
  report.records.push({ ...row, symbol: '600000.SH', name: '未入选对象', selected: false })
  mocks.get.mockResolvedValue(report)
  await render()
  expect(host.querySelectorAll('[data-report-stock]')).toHaveLength(1)
  await click('查看详情')
  for (const text of ['1只观察', '市场中性', '阶段未归类：当前未命中阶段规则', '压力尚未结束', '位置过热：仅作后续跟踪，留意价格回落', '等待下一交易日正式收盘', '不代表盈利预期或买点']) expect(host.textContent).toContain(text)
  for (const text of ['未入选对象', '尚未满足：不过热', '阶段仍待确认']) expect(host.textContent).not.toContain(text)
  expect(host.querySelector('article')?.textContent?.match(/位置过热：仅作后续跟踪，留意价格回落/g)).toHaveLength(1)
})

it.each([
  [{ state: 'lean_strong', phase: 'rally' }, '市场偏强；短线活跃'],
  [{ state: 'neutral', phase: 'unclassified', phase_reason: 'pending_confirmation' }, '阶段候选待确认'],
  [{ state: 'unavailable', phase: 'unknown' }, '市场状态不可用；阶段未知：核心输入不足'],
  [{ phase: 'unclassified' }, '阶段未归类，具体原因未记录'],
])('旧归档市场事实按原含义展示：%j', async (market, sentence) => {
  const report = fixture()
  report.market = market
  mocks.get.mockResolvedValue(report)
  await render()
  await click('查看详情')
  expect(host.textContent).toContain(sentence)
  expect(host.textContent).toContain('当前风险不能计算')
  expect(host.textContent).toContain('位置尚未确认')
  expect(host.querySelectorAll('article')).toHaveLength(1)
})


it.each([
  { label: '全部未知且名单为空', dates: [null, null], selected: false, expected: '分类口径：本轮观察，源生效日未知' },
  { label: '已知日期去重并保留不同来源日期', dates: ['2026-10-28', '2026-10-29', '2026-10-28'], selected: true, expected: '分类口径：本轮观察，分类截至2026-10-28、2026-10-29' },
  { label: '部分来源未知', dates: ['2026-10-28', null], selected: true, expected: '分类口径：本轮观察，分类截至2026-10-28；部分来源生效日未知' },
  { label: '旧归档未记录分类', dates: undefined, selected: false, expected: '分类口径：分类观察时点未记录' },
])('报告顶部保留分类时点说明：$label', async ({ dates, selected, expected }) => {
  const report = fixture()
  if (!selected) report.selected = []
  report.provenance = dates ? { classifications: dates.map(source_effective_date => ({
    source: 'https://example.test/classifications', source_effective_date,
    obtained_at: '2026-10-30 18:00', source_published_at: '2026-10-27',
  })) } : {}
  await act(async () => root.render(<EveningReportContent report={report} reportNotes={[]} />))
  const notes = [...host.querySelectorAll('header p')].filter(p => p.textContent?.startsWith('分类口径：'))
  expect(notes).toHaveLength(1)
  expect(notes[0].textContent).toBe(expected)
  expect(host.textContent?.split(expected)).toHaveLength(2)
  for (const text of ['https://example.test', '2026-10-30 18:00', '2026-10-27']) expect(host.textContent).not.toContain(text)
})

function observation(index = 0): EveningObservation {
  return { role: index % 3 === 2 ? 'emotion' : index % 3 === 1 ? 'fallback_leader' : 'true_leader', role_status: 'qualified', board: index % 2 ? 'main' : 'chinext',
    primary_theme: { id: `theme${Math.floor(index / 3)}`, name: `题材${Math.floor(index / 3)}`, strength: 10 - Math.floor(index / 3) }, related_themes: [],
    values: { raw_close: 10 + index, change_pct: index / 100, return_5d: index / 100, return_20d: index / 10, identity_score: 40 + index, amount_day: 200000000 },
    variables: {}, dip: { status: index % 2 ? 'not_met' : 'pending', reasons: ['首波与事件前置待核验'], gates: {}, e1: {}, e2: {} }, reason: '完整观察理由，不以低吸条件否决', rank: index + 1, risks: ['首波前置待核验'] }
}
function newFixture(count = 3) {
  const report = fixture()
  report.records = Array.from({ length: count }, (_, index) => ({ ...report.records[0], symbol: `0000${String(index).padStart(2, '0')}.SZ`, name: `新版股票${index}`, observation: observation(index) }))
  report.selected = report.records.map(record => record.symbol)
  return report
}
function displayed() { return [...host.querySelectorAll('[data-report-stock]')].map(node => node.getAttribute('data-report-stock')) }
async function feedbackClick(text: string, symbol = '000000.SZ') {
  const row = host.querySelector(`[data-report-stock="${symbol}"]`)?.closest('tr')
  const button = [...row!.querySelectorAll('button')].find(button => button.getAttribute('aria-label')?.includes(text))!
  expect(button).toBeTruthy()
  await act(async () => button.click()); await settle(); await settle()
}

it('新版15只按归档排序；组合筛选、数值排序及恢复都不重选、不改归档', async () => {
  const report = newFixture(15)
  report.records[0].observation!.values.return_5d = null
  report.selected = [...report.selected].reverse()
  const original = JSON.stringify(report)
  mocks.get.mockResolvedValue(report)
  await render()
  expect(displayed()).toEqual(report.selected)
  expect(host.querySelectorAll('table')).toHaveLength(1)
  const requests = mocks.get.mock.calls.length
  const header = [...host.querySelectorAll('th')].find(node => node.textContent?.startsWith('5日涨幅'))!
  await act(async () => header.click())
  expect(displayed()[0]).toBe(report.records[1].symbol)
  expect(displayed().at(-1)).toBe(report.records[0].symbol)
  await act(async () => header.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })))
  expect(displayed()[0]).toBe(report.records[14].symbol)
  expect(displayed().at(-1)).toBe(report.records[0].symbol)
  await click('恢复原报告顺序')
  expect(displayed()).toEqual(report.selected)
  await change('题材', 'theme1'); await change('主角色', 'fallback_leader'); await change('交易板块', 'chinext'); await change('低吸状态', 'pending')
  expect(displayed()).toEqual([report.records[4].symbol])
  await change('搜索股票', '不存在')
  expect(host.textContent).toContain('没有符合当前筛选的股票')
  expect(host.textContent).toContain('本报告仍有 15 只')
  await click('清除筛选')
  expect(displayed()).toEqual(report.selected)
  expect(JSON.stringify(report)).toBe(original)
  expect(mocks.get).toHaveBeenCalledTimes(requests)
  expect(mocks.rebuild).not.toHaveBeenCalled()
})

it('星标与盈亏独立、盈亏互斥且均可取消；反馈不触发行情或自选操作', async () => {
  mocks.get.mockResolvedValue(newFixture())
  await render()
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  await feedbackClick('符合我的审美')
  await feedbackClick('我亏了')
  expect(saved.get('000000.SZ')).toMatchObject({ starred: true, outcome: 'loss' })
  await feedbackClick('我赚了')
  expect(saved.get('000000.SZ')).toMatchObject({ starred: true, outcome: 'profit' })
  await feedbackClick('我赚了')
  expect(saved.get('000000.SZ')).toMatchObject({ starred: true, outcome: null })
  await feedbackClick('符合我的审美')
  expect(saved.get('000000.SZ')).toMatchObject({ starred: false, outcome: null })
  expect(mocks.feedback.mock.calls.map(([request]) => request.expected_revision)).toEqual([0, 1, 2, 3, 4])
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  expect(mocks.add).not.toHaveBeenCalled()
  expect(invalidate.mock.calls.every(([options]) => options?.queryKey?.[0] === 'evening-personal' && options.queryKey[1] === 'test')).toBe(true)
})

it('已星标筛选下取消后立即消失，清除筛选仍保留原推荐', async () => {
  mocks.get.mockResolvedValue(newFixture())
  await render(); await feedbackClick('符合我的审美'); await change('星标', 'starred')
  expect(displayed()).toEqual(['000000.SZ'])
  await feedbackClick('符合我的审美')
  expect(displayed()).toEqual([])
  expect(host.textContent).toContain('当前显示 0 / 本报告 3 只')
  await click('清除筛选')
  expect(displayed()).toHaveLength(3)
  expect(saved.get('000000.SZ')?.starred).toBe(false)
})

it('保存失败保持原状态，重试复用请求标识，保存中禁止快速重复提交', async () => {
  mocks.get.mockResolvedValue(newFixture())
  mocks.feedback.mockRejectedValueOnce(new Error('连接中断'))
  await render(); await feedbackClick('符合我的审美')
  expect(host.textContent).toContain('连接中断')
  const group = host.querySelector('[aria-label="新版股票0的个人反馈"]')!
  expect(group.querySelector('[aria-pressed="true"]')).toBeNull()
  const requestId = mocks.feedback.mock.calls[0][0].request_id
  let resolve!: (value: unknown) => void
  mocks.feedback.mockImplementationOnce(() => new Promise(done => { resolve = done }))
  await click('重试保存')
  expect(mocks.feedback.mock.calls[1][0].request_id).toBe(requestId)
  expect([...group.querySelectorAll('button')].every(button => button.disabled)).toBe(true)
  await act(async () => group.querySelector('button')!.click())
  expect(mocks.feedback).toHaveBeenCalledTimes(2)
  await act(async () => { saved.set('000000.SZ', { starred: true, outcome: null, revision: 1, updated_at: null }); resolve({ episode_id: '000000.SZ', ...saved.get('000000.SZ') }) }); await settle()
  expect(group.querySelector('[aria-pressed="true"]')).not.toBeNull()
})

it('并发冲突重新读取服务器状态，不静默覆盖或重放过期请求', async () => {
  mocks.get.mockResolvedValue(newFixture())
  mocks.feedback.mockImplementationOnce(async () => {
    saved.set('000000.SZ', { starred: false, outcome: 'loss', revision: 2, updated_at: null })
    throw new ApiError('记录版本冲突', 409)
  })
  await render(); await feedbackClick('符合我的审美')
  expect(host.textContent).toContain('记录已在另一处更新')
  const group = host.querySelector('[aria-label="新版股票0的个人反馈"]')!
  expect(group.querySelector('[aria-pressed="true"]')?.textContent).toBe('亏')
  expect(host.textContent).not.toContain('重试保存')
  await feedbackClick('符合我的审美')
  expect(mocks.feedback.mock.calls[1][0]).toMatchObject({ expected_revision: 2, starred: true, outcome: 'loss' })
})

it('个人记录未准备好时按钮禁用，日期切换不保留上一报告筛选', async () => {
  mocks.get.mockImplementation(async (day: string) => ({ ...newFixture(), date: day, trade_date: day }))
  mocks.personal.mockResolvedValue({ user_id: 'test', ready: false, records: [] })
  await render()
  expect(host.textContent).toContain('个人观察记录准备中')
  expect([...host.querySelectorAll('[aria-label="新版股票0的个人反馈"] button')].every(button => (button as HTMLButtonElement).disabled)).toBe(true)
  await change('题材', 'theme0'); await change('主角色', 'emotion')
  expect(displayed()).toHaveLength(1)
  await change('报告日期', '2026-11-01')
  expect(displayed()).toHaveLength(3)
  expect((host.querySelector('[aria-label="主角色"]') as HTMLSelectElement).value).toBe('')
})
