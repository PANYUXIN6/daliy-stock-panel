import { useAiFeature } from '@/lib/useAiCredits'
import type { AiFeature } from '@/lib/api'
import { cn } from '@/lib/cn'

export function AiCreditHint({ feature, className }: { feature: AiFeature; className?: string }) {
  const quota = useAiFeature(feature)
  const reset = quota.data ? new Date(quota.data.resets_at) : null
  const resetLabel = reset && Number.isFinite(reset.getTime())
    ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: 'numeric', day: 'numeric' }).format(reset) : ''
  return <div className={cn('text-[10px] leading-relaxed text-muted', className)} role="status">
    {quota.data ? <>{quota.data.unlimited ? quota.balanceLabel : `每次 ${quota.priceLabel} · ${quota.balanceLabel}${resetLabel ? ` · ${resetLabel}重置` : ''}`}</> : quota.reason}
    {quota.data && !quota.available && <span className="ml-1 text-warning">{quota.reason}</span>}
    {quota.isError && <button type="button" onClick={() => void quota.refetch()} className="ml-1 text-accent hover:underline">重试</button>}
  </div>
}
