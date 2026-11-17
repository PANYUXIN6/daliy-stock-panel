import type { EveningDipStatus, EveningFeedback, EveningObservation } from '@/lib/api'

export const roleLabels: Record<string, string> = { true_leader: '真龙', fallback_leader: '兜底龙', emotion: '情绪股', other: '其他观察' }
export const dipLabels: Record<EveningDipStatus, string> = { full: '完全符合', partial: '部分符合', none: '无低吸点' }
export function dipCategory(dip: EveningObservation['dip']): EveningDipStatus {
  if (dip.status === 'full' || dip.status === 'partial' || dip.status === 'none') return dip.status
  if (['confirmed', 'cautious', 'cautious_confirmed'].includes(dip.status)) return 'full'
  // Old archives retain their facts; missing evidence alone is not a partial match.
  if (dip.status === 'pending' && [dip.e1, dip.e2].some(path => path.value === true
    || Object.values((path.inputs ?? {}) as Record<string, unknown>).some(value => value === true))) return 'partial'
  return 'none'
}
export function dipLabel(dip: EveningObservation['dip']): string { return dipLabels[dipCategory(dip)] }
export const boardLabels: Record<string, string> = { main: '沪深主板', chinext: '创业板' }
export const eveningControl = 'min-h-10 rounded-md border border-border bg-surface px-3 py-2 text-xs text-foreground outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50'
export interface EveningFilterState {
  search: string; theme: string; role: string; roleStatus: string; board: string; dip: string; star: string; outcome: string
}
export const emptyEveningFilters: EveningFilterState = { search: '', theme: '', role: '', roleStatus: '', board: '', dip: '', star: '', outcome: '' }

export function matchesEveningFilters(row: { symbol: string; name: string; observation?: EveningObservation | null }, filters: EveningFilterState, feedback?: EveningFeedback, legacyTheme?: string): boolean {
  const observation = row.observation
  const search = filters.search.trim().toLocaleLowerCase()
  return (!search || `${row.symbol} ${row.name}`.toLocaleLowerCase().includes(search))
    && (!filters.theme || (observation?.primary_theme?.id ?? legacyTheme) === filters.theme)
    && (!filters.role || observation?.role === filters.role)
    && (!filters.roleStatus || observation?.role_status === filters.roleStatus)
    && (!filters.board || observation?.board === filters.board)
    && (!filters.dip || (!!observation && dipCategory(observation.dip) === filters.dip))
    && (!filters.star || (feedback !== undefined && feedback.starred === (filters.star === 'starred')))
    && (!filters.outcome || (feedback !== undefined && feedback.outcome === (filters.outcome === 'unmarked' ? null : filters.outcome)))
}

export function EveningFilters({ value, onChange, onClear, themes, variant = 'report' }: {
  value: EveningFilterState
  onChange: (value: EveningFilterState) => void
  onClear?: () => void
  themes: { id: string; name: string }[]
  variant?: 'report' | 'history'
}) {
  const select = (key: keyof EveningFilterState, label: string, options: Record<string, string>) => <label className="space-y-1 text-xs">
    <span className="block text-secondary">{label}</span>
    <select aria-label={label} value={value[key]} onChange={event => onChange({ ...value, [key]: event.target.value })} className={`${eveningControl} w-full sm:w-auto`}>
      <option value="">全部</option>{Object.entries(options).map(([id, name]) => <option key={id} value={id}>{name}</option>)}
    </select>
  </label>
  return <div className="rounded-lg border border-border bg-elevated/30 p-3 sm:p-4" aria-label="观察名单筛选">
    <div className="grid grid-cols-2 items-end gap-3 sm:flex sm:flex-wrap">
      {variant === 'history' && <label className="col-span-2 space-y-1 text-xs sm:w-44"><span className="block text-secondary">搜索股票</span><input className={`${eveningControl} w-full`} aria-label="搜索股票" placeholder="代码 / 名称" value={value.search} onChange={event => onChange({ ...value, search: event.target.value })} /></label>}
      {select('theme', '题材', Object.fromEntries(themes.map(theme => [theme.id, theme.name])))}
      {select('role', '主角色', { true_leader: '真龙', fallback_leader: '兜底龙', emotion: '情绪股' })}
      {select('roleStatus', '角色状态', { qualified: '当日条件满足', review_pending: '跟踪待复核' })}
      {variant === 'report' && select('board', '交易板块', boardLabels)}
      {variant === 'report' && select('dip', '低吸状态', dipLabels)}
      {variant === 'history' && select('star', '星标', { starred: '已星标', unstarred: '未星标' })}
      {variant === 'history' && select('outcome', '盈亏', { profit: '盈', loss: '亏', unmarked: '未标记' })}
      <button className={`${eveningControl} sm:ml-auto`} onClick={() => onClear ? onClear() : onChange({ ...emptyEveningFilters })}>清除筛选</button>
    </div>
  </div>
}
