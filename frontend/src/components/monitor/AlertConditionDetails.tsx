import type { ReactNode } from 'react'
import type { AlertEvent, MonitorCondition } from '@/lib/api'
import { fmtPrice } from '@/lib/format'
import { cnSignal } from '@/lib/signals'

export function AlertConditionDetails({ event, customNames, fallback }: {
  event: AlertEvent
  customNames?: Record<string, string>
  fallback: ReactNode
}) {
  const exact = (event.matched_conditions?.length ?? 0) > 0
  const signals = event.signals ?? []
  const conditions: MonitorCondition[] = exact ? event.matched_conditions! : (event.conditions ?? [])
  const legacySignals = !exact && signals.length > 0
  const hasDetails = legacySignals || conditions.length > 0
  return <>
    {hasDetails ? (
      <div className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[11px]">
        <span className="text-muted">{exact || legacySignals ? '命中' : '规则'}</span>
        {legacySignals ? signals.map(signal => (
          <span key={signal} className="rounded bg-accent/10 px-1.5 py-0.5 text-[10px] font-medium text-accent">{cnSignal(signal, customNames)}</span>
        )) : conditions.map((condition, index) => (
          <span key={index} className="inline-flex items-center gap-0.5">
            {index > 0 && <span className="text-secondary">{exact || event.logic !== 'or' ? '且' : '或'}</span>}
            <span className={condition.op === 'truth' ? 'text-accent/80' : 'text-foreground/80 font-mono'}>
              {cnSignal(condition.field, customNames)}{condition.op !== 'truth' && `${condition.op}${condition.value}`}
            </span>
          </span>
        ))}
        {event.price != null && <>
          <span className="text-muted">·</span><span className="text-muted">现价</span>
          <span className="font-mono text-foreground/90">{fmtPrice(event.price)}</span>
        </>}
      </div>
    ) : <div className="mt-1 text-[11px]">{fallback}</div>}
    {(exact || legacySignals) && (event.conditions?.length ?? 0) > 0 && (
      <div className="mt-1 flex flex-wrap items-center gap-x-1 gap-y-0.5 text-[10px] text-muted/70">
        <span>规则</span>
        {event.conditions!.map((condition, index) => (
          <span key={index} className="inline-flex items-center gap-0.5">
            {index > 0 && <span>{event.logic === 'or' ? '或' : '且'}</span>}
            <span className={condition.op === 'truth' ? '' : 'font-mono'}>
              {cnSignal(condition.field, customNames)}{condition.op !== 'truth' && `${condition.op}${condition.value}`}
            </span>
          </span>
        ))}
      </div>
    )}
  </>
}
