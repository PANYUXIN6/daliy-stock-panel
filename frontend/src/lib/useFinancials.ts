import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'

export const FINANCIAL_QK = {
  status: ['financials', 'status'],
  executionStatus: ['financials', 'execution-status'],
  metrics: (symbol?: string) => ['financials', 'metrics', symbol],
  income: (symbol?: string) => ['financials', 'income', symbol],
  balanceSheet: (symbol?: string) => ['financials', 'balance-sheet', symbol],
  cashFlow: (symbol?: string) => ['financials', 'cash-flow', symbol],
  shares: (symbol?: string) => ['financials', 'shares', symbol],
}

export function useFinancialStatus() {
  return useQuery({
    queryKey: FINANCIAL_QK.status,
    queryFn: () => api.financialStatus(),
    staleTime: 60_000,
  })
}

export function useFinancialExecutionStatus() {
  return useQuery({
    queryKey: FINANCIAL_QK.executionStatus,
    queryFn: () => api.financialExecutionStatus(),
    staleTime: 60_000,
    // 活动任务状态属于采集服务，离线不会影响本地画像查询。
    refetchInterval: (query) => (query.state.data?.syncing ? 3_000 : false),
  })
}

export function financialMetricsQueryOptions(symbol?: string) {
  return {
    queryKey: FINANCIAL_QK.metrics(symbol),
    queryFn: () => api.financialMetrics(symbol),
    staleTime: 300_000,
  }
}

export function useFinancialMetrics(symbol?: string) {
  return useQuery({ ...financialMetricsQueryOptions(symbol), enabled: !!symbol })
}

export function useFinancialIncome(symbol?: string) {
  return useQuery({
    queryKey: FINANCIAL_QK.income(symbol),
    queryFn: () => api.financialIncome(symbol),
    enabled: !!symbol,
    staleTime: 300_000,
  })
}

export function useFinancialBalanceSheet(symbol?: string) {
  return useQuery({
    queryKey: FINANCIAL_QK.balanceSheet(symbol),
    queryFn: () => api.financialBalanceSheet(symbol),
    enabled: !!symbol,
    staleTime: 300_000,
  })
}

export function useFinancialCashFlow(symbol?: string) {
  return useQuery({
    queryKey: FINANCIAL_QK.cashFlow(symbol),
    queryFn: () => api.financialCashFlow(symbol),
    enabled: !!symbol,
    staleTime: 300_000,
  })
}

export function useFinancialShares(symbol?: string) {
  return useQuery({
    queryKey: FINANCIAL_QK.shares(symbol),
    queryFn: () => api.financialShares(symbol),
    enabled: !!symbol,
    staleTime: 300_000,
  })
}

export function useFinancialSync() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (table: string) => api.financialSync(table),
    // 点击瞬间立即刷新 status: 让后端 is_syncing=True 马上反映到 UI,
    // 避免 mutation 阻塞(全量同步需数分钟)期间界面无变化。
    onMutate: () => {
      qc.invalidateQueries({ queryKey: FINANCIAL_QK.executionStatus })
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: FINANCIAL_QK.executionStatus })
      qc.invalidateQueries({ queryKey: ['financials'] })
    },
  })
}
