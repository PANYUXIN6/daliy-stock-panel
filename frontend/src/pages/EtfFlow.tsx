import { lazy, Suspense, useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { ArrowDownUp, Database, Loader2, RefreshCw } from 'lucide-react'
import { api, type EtfFlowBoard } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useCurrentUser } from '@/lib/auth'
import { useCollectorJob } from '@/lib/useCollectorJob'
import { useCapabilityMatrix } from '@/lib/useSharedQueries'
import { MethodHelp } from '@/components/MethodHelp'
import { FlowChart, type FlowSeries } from './etfFlow/FlowChart'

const EtfReferenceChart = lazy(() => import('./etfFlow/EtfReferenceChart'))

const COLORS = ['#d7a658', '#55a5e8', '#e87676', '#42baa0', '#a38bd4', '#d7bd6f', '#6fa4ac', '#df9470', '#96b776', '#7890d4', '#b58e9e', '#839496']
const yi = (value: number | null | undefined) => value == null ? null : value / 1e8
const money = (value: number | null | undefined) => value == null ? '—' : (value / 1e8).toFixed(2)
const pct = (value: number | null) => value == null ? '—' : `${value > 0 ? '+' : ''}${(value * 100).toFixed(2)}%`
const flowColor = (value: number | null | undefined) => value == null || value === 0 ? 'text-foreground' : value > 0 ? 'text-bull' : 'text-bear'

function cumulative(values: (number | null | undefined)[]) {
  let sum = 0
  let complete = true
  return values.map(value => {
    complete = complete && value != null
    if (!complete || value == null) return null
    sum += value
    return sum
  })
}

function SectionTitle({ id, number, title, detail, help }: { id: string; number: string; title: string; detail?: string; help?: string }) {
  return <div id={id} className="flex scroll-mt-5 flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-border pb-3 pt-5">
    <span className="font-mono text-sm text-accent">{number}</span><div className="flex items-center gap-1"><h2 className="text-lg font-semibold tracking-tight">{title}</h2>{help && <MethodHelp label={`${title}口径`}><p>{help}</p></MethodHelp>}</div>
    {detail && <p className="text-xs text-muted">{detail}</p>}
  </div>
}

function BoardCharts({ data }: { data: EtfFlowBoard }) {
  const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date())
  const currentLabel = data.selected_date === today ? '今日' : data.selected_date
  const charts = useMemo(() => {
    const minutes = data.intraday.points.map(p => p.time)
    const days = data.daily.map(p => p.date)
    const line = (name: string, values: (number | null)[], color: string, extra: Partial<FlowSeries> = {}): FlowSeries => ({ name, data: values, color, ...extra })
    const recent = new Set(data.daily.slice(-3).map(row => row.date))
    const signal = data.daily.filter(row => row.signal || recent.has(row.date))
    const signalCount = data.daily.filter(row => row.signal).length
    const signalCumulative = cumulative(data.signal_average.map(p => yi(p.amount_yuan)))
    const normalCumulative = cumulative(data.normal_average.map(p => yi(p.amount_yuan)))
    const excess = signalCumulative.map((value, i) => value == null || normalCumulative[i] == null ? null : value - normalCumulative[i]!)
    return {
      minutes, days, signal, signalCount,
      current: [line('20只ETF分钟成交额合计', data.intraday.points.map(p => yi(p.amount_yuan)), COLORS[1], { type: 'bar' }),
        line('上证综指', data.intraday.points.map(p => p.index_close), COLORS[0], { rightAxis: true })],
      comparison: [line(currentLabel, data.intraday.points.map(p => yi(p.cumulative_yuan)), COLORS[2]),
        ...(data.previous ? [line(data.previous.date, data.previous.points.map(p => yi(p.cumulative_yuan)), COLORS[0])] : []),
        line('近20日均值', data.average.map(p => yi(p.cumulative_yuan)), COLORS[1], { dashed: true })],
      signalProfile: [line('放量日均值', data.signal_average.map(p => yi(p.amount_yuan)), COLORS[2], { type: 'bar' }),
        line('较常态日多成交', excess, COLORS[0], { rightAxis: true })],
      normalProfile: [line('常态日均值', data.normal_average.map(p => yi(p.amount_yuan)), COLORS[1], { type: 'bar' })],
      signalAmount: [line('当日成交额', signal.map(p => yi(p.amount_yuan)), COLORS[1],
        { type: 'bar', pointColors: signal.map(p => recent.has(p.date) ? COLORS[0] : undefined) })],
      signalFlow: [line('当日净流入', signal.map(p => yi(p.net_flow_yuan)), COLORS[2],
        { type: 'bar', pointColors: signal.map(p => recent.has(p.date) ? COLORS[0] : undefined) })],
      latestAmount: yi(data.daily.slice(-3).findLast(p => p.amount_yuan != null)?.amount_yuan),
      latestFlow: yi(data.daily.slice(-3).findLast(p => p.net_flow_yuan != null)?.net_flow_yuan),
      dailyAmount: [line('20只ETF日度成交额合计', data.daily.map(p => yi(p.amount_yuan)), COLORS[1], { type: 'bar' }),
        line('上证综指', data.daily.map(p => p.index_close), COLORS[0], { rightAxis: true })],
      dailyFlow: [line('20只ETF日度净流入', data.daily.map(p => yi(p.net_flow_yuan)), COLORS[2], { type: 'bar', signed: true }),
        line('上证综指', data.daily.map(p => p.index_close), COLORS[0], { rightAxis: true })],
      broad: [...data.categories.map((category, i) => line(category, data.daily.map(p => yi(p.categories[category]?.net_flow_yuan)), COLORS[i % COLORS.length], { type: 'bar', stack: 'category' })),
        line('累计净流入', cumulative(data.daily.map(p => yi(p.broad?.net_flow_yuan))), COLORS[0], { rightAxis: true })],
    }
  }, [data, currentLabel])
  const amounts = data.intraday.ranking.map(row => row.amount_yuan).filter((n): n is number => n != null)
  const maximum = Math.max(0, ...amounts)
  const heatRows = data.daily.slice(-22).toReversed()
  const heatMaximum = Object.fromEntries(data.categories.map(c => [c, Math.max(0, ...heatRows.map(row => Math.abs(row.categories[c]?.net_flow_yuan ?? 0)))]))
  return <>
    <SectionTitle id="etf-minute" number="01" title="第一部分 · 盘中分钟观测" detail={`${data.selected_date}${data.intraday.as_of ? ` · ${data.intraday.as_of}` : ''}`} />
    <div className="grid gap-4 xl:grid-cols-2">
      <FlowChart title="20只ETF分钟成交额和上证综指分时" labels={charts.minutes} series={charts.current} />
      <FlowChart title="20只ETF当日分钟累计成交额（亿元）" labels={charts.minutes} series={charts.comparison} />
    </div>
    <h3 className="mt-2 text-sm font-semibold">20大ETF放量案例</h3>
    <div className="grid gap-4 xl:grid-cols-2">
      <FlowChart title="放量案例日 · 重点ETF分钟成交额" labels={data.signal_average.map(p => p.time)} series={charts.signalProfile}
        leftAxisName="亿元/分钟" rightAxisName="较常态日多成交（亿元）" zoom />
      <FlowChart title="常态交易日 · 重点ETF分钟成交额" labels={data.normal_average.map(p => p.time)} series={charts.normalProfile} leftAxisName="亿元/分钟" zoom />
    </div>
    <section className="overflow-hidden rounded-card border border-border bg-surface" aria-label="单只ETF表现">
      <div className="flex items-center gap-1 px-4 py-3"><h3 className="text-sm font-semibold">单只ETF当日表现</h3><MethodHelp label="单只ETF表现口径"><p>按成交额降序。条形背景表示成交额相对最大值的比例，涨跌幅按未复权价格计算。</p></MethodHelp></div>
      <div className="overflow-x-auto"><table className="w-full min-w-[650px] text-xs">
        <thead className="border-y border-border bg-elevated/60 text-muted"><tr><th className="px-4 py-2 text-left">代码</th><th className="px-3 py-2 text-left">名称</th><th className="px-3 py-2 text-right">当日成交额(亿)</th><th className="px-3 py-2 text-right">占比</th><th className="px-3 py-2 text-right">涨跌幅</th></tr></thead>
        <tbody>{data.intraday.ranking.map(row => <tr key={row.symbol} className="border-b border-border/60 last:border-0 hover:bg-elevated/50">
          <td className="px-4 py-2.5 font-mono text-muted">{row.symbol.split('.')[0]}</td>
          <td className="px-3 py-2">{row.name}</td>
          <td className="relative px-3 py-2 text-right font-mono"><span aria-hidden className="absolute inset-y-1 right-1 rounded-sm bg-accent/10" style={{ width: `${maximum && row.amount_yuan != null ? row.amount_yuan / maximum * 95 : 0}%` }} /><span className="relative">{money(row.amount_yuan)}</span></td>
          <td className="px-3 py-2 text-right font-mono">{row.weight == null ? '—' : `${(row.weight * 100).toFixed(1)}%`}</td>
          <td className={`px-3 py-2 text-right font-mono ${flowColor(row.change_pct)}`}>{pct(row.change_pct)}</td>
        </tr>)}</tbody>
      </table></div>
    </section>

    <SectionTitle id="etf-signals" number="02" title="第二部分 · ETF放量信号" help="放量日指单日净流入超过100亿元（申赎口径）。" />
    <div className="grid gap-4 xl:grid-cols-2">
      <FlowChart title={`日度成交额：${charts.signalCount}个放量日（蓝） + 最近3个交易日（黄）· 时间序`} labels={charts.signal.map(p => p.date)} series={charts.signalAmount} referenceValue={charts.latestAmount} showLegend={false} zoom />
      <FlowChart title={`日度净流入：${charts.signalCount}个放量日（红） + 最近3个交易日（黄）· 时间序`} labels={charts.signal.map(p => p.date)} series={charts.signalFlow} referenceValue={charts.latestFlow} showLegend={false} zoom />
    </div>

    <SectionTitle id="etf-daily" number="03" title="第三部分 · 日度资金全景" help="净流入按申赎口径估算：（当日份额 − 上一交易日份额）× 当日单位净值。" />
    <div className="grid gap-4 xl:grid-cols-2">
      <FlowChart title="20只ETF日度成交额与上证综指" labels={charts.days} series={charts.dailyAmount} zoom />
      <FlowChart title="20只ETF日度净流入（申赎口径）与上证综指" labels={charts.days} series={charts.dailyFlow} zoom />
    </div>
    <FlowChart title="宽基ETF日度净流入与累计净流入" labels={charts.days} series={charts.broad}
      leftAxisName="当日净流入（亿元）" rightAxisName="累计净流入（亿元）" help="统计已识别宽基ETF。累计从当前显示区间首日开始。" zoom />
    <section className="overflow-hidden rounded-card border border-border bg-surface" aria-label="宽基净申购热力表">
      <div className="flex items-center gap-1 px-4 py-3"><h3 className="text-sm font-semibold">最近一个月 · 各类宽基ETF日度净流入（亿元）</h3><MethodHelp label="宽基净申购热力表口径"><p>合计为已识别类别之和。红色表示净申购，绿色表示净赎回，颜色深浅按各列的相对大小显示。</p></MethodHelp></div>
      <div className="overflow-x-auto"><table className="w-full min-w-[1180px] text-xs">
        <thead className="bg-elevated text-muted"><tr><th className="sticky left-0 z-10 bg-elevated px-3 py-2 text-left">日期</th><th className="whitespace-nowrap px-2 py-2 text-right">合计</th>{data.categories.map(c => <th key={c} className="whitespace-nowrap px-2 py-2 text-right">{c}</th>)}</tr></thead>
        <tbody>{heatRows.map(row => <tr key={row.date} className="border-t border-border/50"><th className="sticky left-0 z-10 whitespace-nowrap bg-surface px-3 py-2 text-left font-mono font-normal">{row.date}</th><td className={`px-2 py-2 text-right font-mono ${flowColor(row.broad?.net_flow_yuan)}`}>{money(row.broad?.net_flow_yuan)}</td>{data.categories.map(category => {
          const cell = row.categories[category]
          const value = cell?.net_flow_yuan
          const strength = value == null || !heatMaximum[category] ? 0 : Math.abs(value) / heatMaximum[category]
          return <td key={category} className="px-2 py-2 text-right font-mono" style={{ backgroundColor: value == null ? undefined : `rgba(${value >= 0 ? '220,70,70' : '30,160,110'},${0.04 + strength * 0.23})` }}>
            <span className={flowColor(value)}>{value != null && value > 0 ? '+' : ''}{money(value)}</span>
          </td>
        })}</tr>)}</tbody>
      </table></div>
      {!heatRows.length && <p className="p-8 text-center text-xs text-muted">暂无日度资金数据</p>}
    </section>
  </>
}

export function EtfFlow() {
  const user = useCurrentUser()
  const admin = user?.role === 'admin'
  const qc = useQueryClient()
  const [date, setDate] = useState('')
  const [window, setWindow] = useState(22)
  const [jobId, setJobId] = useState<string | null>(null)
  const board = useQuery({ queryKey: QK.etfFlow(date || undefined, window), queryFn: () => api.etfFlow(date || undefined, window), staleTime: 15_000 })
  const etfReference = useQuery({ queryKey: QK.etfReference(date || undefined),
    queryFn: () => api.etfReference(date || undefined), staleTime: 60 * 60_000 })
  const capability = useCapabilityMatrix()
  const missing = capability.data?.capabilities.filter(c => ['daily', 'minute', 'fund_nav'].includes(c.id) && !c.usable) ?? []
  const jobs = useQuery({ queryKey: QK.pipelineJobs, queryFn: () => api.pipelineJobs(), enabled: admin })
  useEffect(() => {
    const active = jobs.data?.jobs.find(job => job.request_key === 'etf_flow' && ['pending', 'running'].includes(job.status))
    if (active) setJobId(active.id)
  }, [jobs.data])
  const job = useCollectorJob(jobId, () => { void qc.invalidateQueries({ queryKey: QK.etfFlowAll }) })
  const sync = useMutation({ mutationFn: (fullHistory: boolean) => api.syncEtfFlow(window || 22, fullHistory), onSuccess: response => { setJobId(response.job_id); void qc.invalidateQueries({ queryKey: QK.pipelineJobs }) } })
  const data = board.data
  const latestFlow = data?.daily.findLast(row => row.net_flow_yuan != null)
  const latestMinute = data?.intraday.points.find(p => p.time === data.intraday.as_of)
  const reference = data?.average.find(p => p.time === data.intraday.as_of)
  const relative = latestMinute?.cumulative_yuan != null && reference?.cumulative_yuan != null && reference.cumulative_yuan > 0
    ? latestMinute.cumulative_yuan / reference.cumulative_yuan - 1 : null
  const button = 'inline-flex items-center justify-center gap-1.5 rounded-btn border border-border px-3 py-2 text-xs transition-colors hover:bg-elevated disabled:cursor-not-allowed disabled:opacity-50'
  const refreshing = board.isFetching || etfReference.isFetching
  return <main className="mx-auto flex w-full max-w-[1600px] flex-col gap-4 p-3 text-foreground sm:p-6">
    <header className="flex flex-wrap items-end justify-between gap-4 border-b border-border pb-5">
      <div><p className="mb-2 flex items-center gap-2 font-mono text-[10px] tracking-[0.2em] text-accent"><ArrowDownUp className="h-3 w-3" /> ETF / FUND FLOWS</p><h1 className="text-2xl font-semibold tracking-tight">重点ETF资金看板</h1></div>
      <div className="flex flex-wrap items-end gap-2">
        <label className="text-[10px] text-muted">观察日期<select aria-label="观察日期" value={date} onChange={e => setDate(e.target.value)} className="mt-1 block rounded-btn border border-border bg-surface px-2 py-2 text-xs text-foreground"><option value="">最新交易日</option>{data?.dates.toReversed().map(d => <option key={d} value={d}>{d}</option>)}</select></label>
        <label className="text-[10px] text-muted">历史窗口<select aria-label="历史窗口" value={window} onChange={e => setWindow(Number(e.target.value))} className="mt-1 block rounded-btn border border-border bg-surface px-2 py-2 text-xs text-foreground">{[22, 60, 120, 0].map(n => <option key={n} value={n}>{n ? `${n} 个交易日` : '全部历史'}</option>)}</select></label>
        <button className={button} disabled={refreshing} onClick={() => { void board.refetch(); void etfReference.refetch() }}><RefreshCw className={`h-3.5 w-3.5 ${refreshing ? 'animate-spin' : ''}`} />刷新</button>
        {admin && <button className={`${button} border-accent/40 text-accent`} disabled={sync.isPending || job.running} onClick={() => sync.mutate(false)}><Database className="h-3.5 w-3.5" />{sync.isPending || job.running ? '同步中…' : '同步数据'}</button>}
        {admin && <button className={button} disabled={sync.isPending || job.running} onClick={() => sync.mutate(true)}>补采历史</button>}
      </div>
    </header>
    <nav aria-label="看板分区" className="flex flex-wrap gap-x-6 gap-y-2 text-xs text-muted"><a className="hover:text-accent" href="#etf-minute">01 盘中分钟观测</a><a className="hover:text-accent" href="#etf-signals">02 ETF放量信号</a><a className="hover:text-accent" href="#etf-daily">03 日度资金全景</a></nav>
    {admin && missing.length > 0 && <p className="rounded-btn border border-warning/25 bg-warning/5 p-3 text-xs text-warning">请配置：{missing.map(c => c.label).join('、')}。<Link to="/settings?tab=data-sources" className="ml-2 underline">检查数据源配置</Link></p>}
    {sync.isError && <p role="alert" className="text-xs text-bear">{sync.error.message}</p>}
    {jobId && <div role="status" className="flex flex-wrap items-center justify-between gap-2 rounded-btn border border-border bg-elevated p-3 text-xs"><span>{job.running && <Loader2 className="mr-2 inline h-3 w-3 animate-spin" />}{job.isError ? '任务状态读取失败' : job.message}</span>{job.running ? <button className="text-accent" disabled={job.cancel.isPending} onClick={() => job.cancel.mutate()}>取消同步</button> : <button className="text-muted" onClick={() => setJobId(null)}>收起</button>}{job.isError && <button className="text-accent" onClick={() => void job.refetch()}>重试读取</button>}{job.cancel.isError && <span className="text-bear">取消失败，请重试</span>}</div>}
    {board.isError && <p role="alert" className="rounded-btn border border-bear/25 p-4 text-sm text-bear">看板读取失败：{board.error.message}<button className="ml-3 underline" onClick={() => void board.refetch()}>重试</button></p>}
    {board.isPending && <div role="status" className="flex h-64 items-center justify-center gap-2 text-muted"><Loader2 className="h-4 w-4 animate-spin" />加载ETF资金数据…</div>}
    {data && <>
      {data.state === 'no_data' && <p className="rounded-card border border-border p-6 text-center text-sm text-muted">{admin ? '暂无数据，点击“同步数据”开始。' : '暂无数据。'}</p>}
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-card border border-border bg-border lg:grid-cols-4">
        {[
          { label: '盘中累计成交额', value: money(data.intraday.amount_yuan), unit: '亿元', sub: `${data.selected_date} ${data.intraday.as_of ?? ''}`, color: '' },
          { label: '较前20日同刻均值', value: pct(relative), unit: '', sub: '', color: flowColor(relative) },
          { label: '最新净流入', value: money(latestFlow?.net_flow_yuan), unit: '亿元', sub: latestFlow?.date ?? '', color: flowColor(latestFlow?.net_flow_yuan) },
          { label: '区间放量日', value: data.coverage.flow_days ? String(data.daily.filter(row => row.signal).length) : '—', unit: '日', sub: '', color: '' },
        ].map(item => <div key={item.label} className="bg-surface p-4 sm:p-5"><p className="text-[11px] text-muted">{item.label}</p><p className={`my-2 font-mono text-xl font-medium sm:text-2xl ${item.color}`}>{item.value}<span className="ml-2 font-sans text-[10px] font-normal text-muted">{item.unit}</span></p>{item.sub && <p className="text-[10px] leading-relaxed text-muted">{item.sub}</p>}</div>)}
      </div>
      <BoardCharts data={data} />
    </>}
    <Suspense fallback={<div className="h-[280px] rounded-card border border-border bg-surface" />}>
      <EtfReferenceChart data={etfReference.data} loading={etfReference.isLoading}
        error={etfReference.isError} onRetry={() => { void etfReference.refetch() }} />
    </Suspense>
  </main>
}
