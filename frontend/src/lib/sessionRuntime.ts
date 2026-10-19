import { applyPageScale } from './pageScale'

/** 当前页面只承载一个身份；身份变化后重载，销毁后台任务与模块级缓存。 */
let owner: string | null = null
let blocked = false
const controllers = new Set<AbortController>()
const streams = new Set<EventSource>()
export const AUTH_CHANGED_KEY = 'tf-auth-changed'

export function activateSession(id: string) { owner = id; blocked = false }
export function isSessionLocked() { return blocked }
export function lockSession() {
  if (blocked) return
  blocked = true
  for (const controller of controllers) controller.abort()
  controllers.clear()
  for (const stream of streams) stream.close()
  streams.clear()
  window.speechSynthesis?.cancel()
  window.dispatchEvent(new Event('session-locked'))
}
export function announceSessionChange() {
  try { localStorage.setItem(AUTH_CHANGED_KEY, `${Date.now()}:${Math.random()}`) } catch { /* 存储禁用时由后续业务请求检测会话变化 */ }
}
export const accountStorage = {
  getItem(key: string): string | null { try { return owner && !blocked ? localStorage.getItem(`tf-user:${owner}:${key}`) : null } catch { return null } },
  setItem(key: string, value: string) { try { if (owner && !blocked) localStorage.setItem(`tf-user:${owner}:${key}`, value) } catch { /* 存储不可用 */ } },
  removeItem(key: string) { try { if (owner && !blocked) localStorage.removeItem(`tf-user:${owner}:${key}`) } catch { /* 存储不可用 */ } },
}

/** JSON、下载与 fetch 流统一处理 HTTP 状态，失效后禁止后台继续写入。 */
export async function sessionFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = typeof input === 'string' || input instanceof URL ? String(input) : input.url
  const path = new URL(url, window.location.origin).pathname.replace(/\/$/, '')
  const unbound = path === '/api/auth/status' || path === '/api/auth/login'
  if ((!owner || blocked) && !unbound) throw new DOMException('身份未确认或会话已锁定', 'AbortError')
  const headers = new Headers(init?.headers ?? (typeof input !== 'string' && !(input instanceof URL) ? input.headers : undefined))
  if (unbound) headers.delete('X-Expected-User')
  else headers.set('X-Expected-User', owner!)
  const controller = new AbortController()
  const abort = () => controller.abort()
  init?.signal?.addEventListener('abort', abort, { once: true })
  if (init?.signal?.aborted) controller.abort()
  if (!unbound) controllers.add(controller)
  const release = () => { controllers.delete(controller); init?.signal?.removeEventListener('abort', abort) }
  try {
    const response = await fetch(input, { ...init, headers, signal: controller.signal })
    if (response.status === 401 && !unbound) {
      release()
      lockSession()
      return response
    }
    if (!response.body || unbound) { release(); return response }
    // pipeTo 负责正常 EOF、取消和错误时释放原响应 reader；响应体仍受会话 controller 控制。
    const body = new TransformStream<Uint8Array, Uint8Array>()
    void response.body.pipeTo(body.writable, { signal: controller.signal }).catch(() => {
      // 错误已传递到 readable，交给现有 JSON / 流消费方显示。
    }).finally(release)
    return new Response(body.readable, { status: response.status, statusText: response.statusText, headers: response.headers })
  } catch (error) { release(); throw error }

}

export function sessionEventSource(url: string): EventSource {
  if (!owner || blocked) throw new DOMException('身份未确认或会话已锁定', 'AbortError')
  const endpoint = new URL(url, window.location.origin)
  endpoint.searchParams.set('expected_user_id', owner)
  const stream = new EventSource(endpoint.toString())
  streams.add(stream)
  const close = stream.close.bind(stream)
  stream.close = () => { streams.delete(stream); close() }
  stream.addEventListener('error', () => window.dispatchEvent(new Event('session-check')))
  stream.addEventListener('auth_expired', lockSession)
  return stream
}

/** 旧浏览器数据只归属于后端明确识别的初始管理员；缓存不迁移。 */
export function migrateInitialAdminStorage(id: string) {
  try {
  const marker = `tf-user:${id}:legacy-migrated`
  if (localStorage.getItem(marker)) return
  const keys = Object.keys(localStorage)
  for (const key of keys) {
    if (key.startsWith('tf-user:') || key === AUTH_CHANGED_KEY || key === 'tf-query-cache') continue
    const value = localStorage.getItem(key)
    const target = `tf-user:${id}:${key}`
    if (value !== null && localStorage.getItem(target) === null) localStorage.setItem(target, value)
  }
  localStorage.setItem(marker, '1')
  } catch { /* 保留旧数据和未完成标记，下次登录继续迁移。 */ }
}

export function applyAccountAppearance() {
  document.documentElement.classList.toggle('dark', accountStorage.getItem('tf-theme') !== 'light')
  let zoom = 1
  try { const value = JSON.parse(accountStorage.getItem('tf-page-zoom') || '1'); if (typeof value === 'number' && value >= 0.8 && value <= 1.5) zoom = value } catch { /* 默认缩放 */ }
  applyPageScale(zoom)
}
