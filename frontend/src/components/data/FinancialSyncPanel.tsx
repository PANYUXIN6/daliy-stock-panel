import { Link } from 'react-router-dom'
import { useFinancialStatus } from '@/lib/useFinancials'

export function FinancialSyncPanel() {
  const status = useFinancialStatus()
  return (
    <div className="space-y-3 text-xs">
      <h4 className="font-medium text-foreground">财务数据按需获取</h4>
      <p className="text-muted">搜索股票后可获取所需报表；策略和因子任务会准备所需财务数据。保存后所有用户共享复用。</p>
      <p className="text-muted">已保存利润表：{(status.data?.tables.income?.rows ?? 0).toLocaleString()} 条</p>
      <Link to="/financials" className="inline-block rounded-btn bg-accent px-3 py-2 text-white">搜索股票并获取财务数据</Link>
      {status.isError && <p role="alert" className="text-danger">{status.error.message}</p>}
    </div>
  )
}
