import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { HelpCircle } from 'lucide-react'

export function MethodHelp({ label, children }: { label: string; children: ReactNode }) {
  const id = useId()
  const button = useRef<HTMLButtonElement>(null)
  const tooltip = useRef<HTMLDivElement>(null)
  const [anchor, setAnchor] = useState<DOMRect | null>(null)
  const open = anchor !== null
  const show = () => setAnchor(button.current?.getBoundingClientRect() ?? null)
  const close = () => setAnchor(null)

  useEffect(() => {
    if (!open) return
    const dismissOutside = (event: PointerEvent) => {
      const target = event.target as Node
      if (!button.current?.contains(target) && !tooltip.current?.contains(target)) close()
    }
    const dismissOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.stopPropagation()
      close()
    }
    const dismissOnScroll = (event: Event) => {
      if (event.target !== tooltip.current) close()
    }
    document.addEventListener('pointerdown', dismissOutside)
    document.addEventListener('keydown', dismissOnEscape, true)
    window.addEventListener('scroll', dismissOnScroll, true)
    window.addEventListener('resize', close)
    return () => {
      document.removeEventListener('pointerdown', dismissOutside)
      document.removeEventListener('keydown', dismissOnEscape, true)
      window.removeEventListener('scroll', dismissOnScroll, true)
      window.removeEventListener('resize', close)
    }
  }, [open])

  const width = open ? Math.min(320, window.innerWidth - 16) : 0
  const below = anchor ? window.innerHeight - anchor.bottom : 0
  const above = anchor !== null && below < 180 && anchor.top > below
  return (
    <span className="inline-flex shrink-0"
      onPointerEnter={event => { if (event.pointerType === 'mouse') show() }}
      onPointerLeave={event => { if (event.pointerType === 'mouse') close() }}>
      <button ref={button} type="button" aria-label={label} aria-expanded={open}
        aria-describedby={open ? id : undefined} onFocus={show} onBlur={close} onClick={show}
        className="inline-flex size-6 items-center justify-center rounded text-muted hover:text-accent focus-visible:text-accent focus-visible:outline focus-visible:outline-accent">
        <HelpCircle aria-hidden="true" className="size-3.5" />
      </button>
      {anchor && createPortal(
        <div ref={tooltip} id={id} role="tooltip"
          style={{ width, left: Math.max(8, Math.min(anchor.left, window.innerWidth - width - 8)),
            ...(above ? { bottom: window.innerHeight - anchor.top } : { top: anchor.bottom }),
            maxHeight: (above ? anchor.top : below) - 8 }}
          className="fixed z-[70] space-y-1.5 overflow-y-auto rounded-md border border-border bg-surface p-3 text-xs leading-relaxed text-secondary shadow-xl">
          {children}
        </div>, document.body)}
    </span>
  )
}
