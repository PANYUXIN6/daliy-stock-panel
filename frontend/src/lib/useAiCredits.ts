import { useEffect } from 'react'
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { api, type AiFeature } from './api'
import { useCurrentUser } from './auth'
import { rememberAiCreditPreview, subscribeAiCredits } from './aiGeneration'
import { QK } from './queryKeys'

// 同一账号的所有入口共享查询与一条事件订阅，避免多张卡片重复刷新。
const subscriptions = new WeakMap<QueryClient, Map<string, { count: number; stop: () => void }>>()

function subscribe(client: QueryClient, userId: string) {
  let users = subscriptions.get(client)
  if (!users) { users = new Map(); subscriptions.set(client, users) }
  let entry = users.get(userId)
  if (!entry) {
    const key = QK.aiCredits(userId)
    entry = { count: 0, stop: subscribeAiCredits(event => {
      if (event.type === 'snapshot') client.setQueryData(key, event.credits)
      else void client.invalidateQueries({ queryKey: key })
    }) }
    users.set(userId, entry)
  }
  entry.count++
  return () => {
    if (--entry.count === 0) { entry.stop(); users.delete(userId) }
  }
}

export function useAiCredits(enabled = true) {
  const user = useCurrentUser()
  const client = useQueryClient()
  const userId = user?.id ?? ''
  useEffect(() => userId ? subscribe(client, userId) : undefined, [client, userId])
  const query = useQuery({
    queryKey: QK.aiCredits(userId),
    queryFn: ({ signal }) => api.aiCredits(signal),
    enabled: Boolean(userId) && enabled,
    staleTime: 15_000,
    refetchOnWindowFocus: true,
    refetchInterval: query => {
      // 取消后的首次刷新可能早于服务端退点结算；预留归零前短轮询，随后停止。
      if ((query.state.data?.reserved_points ?? 0) > 0) return 2_000
      const reset = Date.parse(query.state.data?.resets_at ?? '')
      return Number.isFinite(reset) ? Math.min(Math.max(reset - Date.now(), 1_000), 2_147_483_647) : false
    },
    retry: false,
  })
  useEffect(() => { if (query.data && enabled) rememberAiCreditPreview(query.data) }, [query.data, enabled])
  return query
}

export function useAiFeature(feature: AiFeature, enabled = true) {
  const query = useAiCredits(enabled)
  const credits = query.data
  const access = credits?.features[feature]
  const available = Boolean(access?.available) && !query.isError
  const reason = query.isError ? 'AI 额度读取失败，请刷新重试' : access?.reason || (!credits ? '正在读取 AI 额度' : !available ? '当前 AI 额度不可用' : null)
  const priceLabel = credits?.unlimited ? '不限额' : access ? `${access.points} 点` : '额度加载中'
  const balanceLabel = credits?.unlimited ? '管理员不限额，不扣点' : credits ? `本月剩余 ${credits.remaining_points ?? 0} / ${credits.monthly_points ?? 0} 点` : ''
  return { ...query, available, reason, priceLabel, balanceLabel }
}
