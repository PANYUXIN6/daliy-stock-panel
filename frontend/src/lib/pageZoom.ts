/** 账号专属页面缩放：认证恢复与设置使用同一个 DOM 实现。 */
import { storage } from '@/lib/storage'
import { applyPageScale, ZOOM_DEFAULT, ZOOM_MAX, ZOOM_MIN } from './pageScale'

export { getPageScale, ZOOM_DEFAULT, ZOOM_MAX, ZOOM_MIN, ZOOM_STEP } from './pageScale'
export const applyPageZoom = applyPageScale

export function getPageZoom(): number {
  const value = storage.pageZoom.get(ZOOM_DEFAULT)
  return typeof value === 'number' && Number.isFinite(value)
    ? Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(value * 100) / 100))
    : ZOOM_DEFAULT
}

export function setPageZoom(value: number): number {
  const zoom = applyPageScale(value)
  storage.pageZoom.set(zoom)
  return zoom
}
