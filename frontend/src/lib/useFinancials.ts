import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type FinancialTable } from '@/lib/api'

export const FINANCIAL_QK = {
  status: ['financials', 'status'],
  collectionOptions: ['financials', 'collection-options'],
  metrics: (symbol?: string) => ['financials', 'metrics', symbol],
  income: (symbol?: string) => ['financials', 'income', symbol],
  balanceSheet: (symbol?: string) => ['financials', 'balance-sheet', symbol],
  cashFlow: (symbol?: string) => ['financials', 'cash-flow', symbol],
  shares: (symbol?: string) => ['financials', 'shares', symbol],
}

export function useFinancialCollectionOptions() {
  return useQuery({ queryKey: FINANCIAL_QK.collectionOptions, queryFn: api.financialCollectionOptions, staleTime: 30_000 })
}

export function useFinancialFetch() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ symbol, table, before }: { symbol: string; table: FinancialTable; before?: string }) =>
      before ? api.financialFetch(symbol, table, before) : api.financialFetch(symbol, table),
    onSuccess: (_result, { symbol, table }) => {
      const key = {
        metrics: FINANCIAL_QK.metrics, income: FINANCIAL_QK.income,
        balance_sheet: FINANCIAL_QK.balanceSheet, cash_flow: FINANCIAL_QK.cashFlow, shares: FINANCIAL_QK.shares,
      }[table](symbol)
      return Promise.all([
        qc.invalidateQueries({ queryKey: key }),
        qc.invalidateQueries({ queryKey: FINANCIAL_QK.status }),
      ])
    },
  })
}

export function useFinancialStatus() {
  return useQuery({
    queryKey: FINANCIAL_QK.status,
    queryFn: () => api.financialStatus(),
    staleTime: 60_000,
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
