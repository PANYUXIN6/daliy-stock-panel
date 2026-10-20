import { isSessionLocked, sessionFetch } from './sessionRuntime'

export type AiFeature = 'assistant' | 'stock_analysis' | 'financial_analysis' | 'market_recap' | 'concept_rotation' | 'industry_rotation'

export interface AiCredits {
  policy_version: number
  period: string
  resets_at: string
  role: 'guest' | 'pro' | 'admin'
  unlimited: boolean
  monthly_points: number | null
  used_points: number
  reserved_points: number
  remaining_points: number | null
  cost_limit_cny: string | null
  cost_limits_enabled?: boolean
  cost_used_cny: string
  features: Record<AiFeature, { points: number; available: boolean; reason: string | null }>
}

type CreditsEvent = { type: 'snapshot'; credits: AiCredits } | { type: 'refresh' }
const listeners = new Set<(event: CreditsEvent) => void>()
let preview: AiCredits | null = null

/** 只记录已交给界面展示的规则，预检读取不能自行覆盖用户看到的价格。 */
export function rememberAiCreditPreview(credits: AiCredits) { preview = credits }

function requestId() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  // 局域网 HTTP 页面中 randomUUID 可能不可用，getRandomValues 仍可用。
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = [...bytes].map(byte => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

function publish(event: CreditsEvent) {
  if (!isSessionLocked()) for (const listener of listeners) listener(event)
}

export function subscribeAiCredits(listener: (event: CreditsEvent) => void) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

export async function fetchAiCredits(signal?: AbortSignal): Promise<AiCredits> {
  const response = await sessionFetch('/api/ai/credits', { signal })
  if (!response.ok) {
    let message = 'AI 额度暂时无法读取，请稍后重试'
    try { const body = await response.json(); if (typeof body.detail === 'string') message = body.detail } catch { /* 使用默认文案 */ }
    throw new Error(message)
  }
  return response.json() as Promise<AiCredits>
}

/** 每个显式任务只生成一个请求标识，内部工具轮次与重试沿用后端任务。 */
export async function* runAiGeneration<T>(
  feature: AiFeature,
  generate: (headers: Record<string, string>) => AsyncGenerator<T>,
  signal?: AbortSignal,
): AsyncGenerator<T> {
  const expected = preview
  try {
    const credits = await fetchAiCredits(signal)
    if (signal?.aborted) throw new DOMException('已取消生成', 'AbortError')
    publish({ type: 'snapshot', credits })
    if (expected && (expected.policy_version !== credits.policy_version
      || expected.unlimited !== credits.unlimited
      || expected.features[feature]?.points !== credits.features[feature]?.points)) {
      throw new Error('AI 点数规则已更新，请确认最新点数后重新发起；本次未扣点。')
    }
    const access = credits.features[feature]
    if (!access?.available) throw new Error(access?.reason || '当前 AI 额度不可用')
    const headers = {
      'X-AI-Request-ID': requestId(),
      'X-AI-Policy-Version': String(credits.policy_version),
    }
    let started = false
    for await (const event of generate(headers)) {
      if (!started) { started = true; publish({ type: 'refresh' }) }
      yield event
    }
  } finally {
    // 成功、失败、取消与提前退出均重新读取服务端余额；前端不自行加减点数。
    publish({ type: 'refresh' })
  }
}
